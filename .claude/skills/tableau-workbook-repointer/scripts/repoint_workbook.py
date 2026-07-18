#!/usr/bin/env python3
"""Repoint workbooks to new PDS by TWB text surgery + republish.

repoint mode. For each selected workbook in repoint-design.json:

  1. download the workbook (no extract; .twbx is unpacked to its single .twb)
  2. rewrite every reference to each old PDS by attribute-scoped text
     replacement over the whole file (which also covers the copies inside the
     capabilities-cache CDATA blob — its serialized XML uses the same
     attribute syntax):
       - content_url attrs : `id='<tok>'` / `dbname='<tok>'` /
         `/datasources/<tok>?` (repository-location, connection, derived-from)
       - display-name attrs: `caption='<name>'` / `server-ds-friendly-name='<name>'`
     Attribute-scoped (not indiscriminate substring) because a PDS whose
     display name EQUALS its content_url would otherwise get its caption
     overwritten with the new content_url instead of the new display name.
     Worksheet field references use the internal `sqlproxy.<hash>` datasource
     name, which is left untouched, so views keep working.
     The old token is taken from what the TWB actually references: the old
     PDS's current content_url when present, else the repository-location id
     of the datasource whose caption matches the old display name (workbooks
     can keep referencing a stale content_url after a PDS was republished
     under a suffixed one).
  3. republish the modified .twb (PublishMode.Overwrite):
       - stage=rehearsal : into the rehearsal project under a prefixed name
         (idempotent — re-runs overwrite the same rehearsal copy)
       - stage=production: over the original workbook (same project + name;
         the workbook LUID and webpage URL are preserved by overwrite)
  4. post-publish check via REST connections. Primary signal: no connection
     may still carry an old display name or old token. Secondary: each new
     PDS must appear as its display name OR its content_url — REST
     `datasource_name` can return either, and `datasource id` is a shadow id,
     not the PDS LUID.

New content_urls are resolved live from the LUIDs in the design (they are not
guessable from display names). Pairs whose LUIDs are null are rejected — run
publish_manifest.py resolve-luids first.

Usage:
    python repoint_workbook.py --design <repoint-design.json> \
        (--workbook <wb_luid> ... | --all) \
        --stage rehearsal --rehearsal-project <name-or-luid> \
        --work-dir <dir>
    python repoint_workbook.py --design ... (--workbook ... | --all) \
        --stage production --work-dir <dir>

Server writes: workbook publish only. Final line: RESULT_JSON: {...}
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))

import tableauserverclient as TSC  # noqa: E402
from tableau_auth import signed_in_server  # noqa: E402

# Rehearsal copies are named <prefix><original name> so they sort together and
# are recognizable as disposable in the rehearsal project.
DEFAULT_REHEARSAL_PREFIX = "rehearsal_"


def slug(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name).strip("_").lower()


def resolve_project(server, name_or_luid: str) -> str:
    """Return a project LUID from a LUID or a unique project name."""
    projects = list(TSC.Pager(server.projects))
    for p in projects:
        if p.id == name_or_luid:
            return p.id
    hits = [p for p in projects if p.name == name_or_luid]
    if len(hits) == 1:
        return hits[0].id
    if not hits:
        sys.exit(f"ERROR: rehearsal project not found: {name_or_luid!r}")
    sys.exit(f"ERROR: rehearsal project name {name_or_luid!r} is ambiguous "
             f"({len(hits)} matches); pass its LUID instead")


def extract_twb(downloaded: Path, dest_dir: Path) -> Path:
    """Return the .twb path; unpack .twbx (must contain exactly one .twb)."""
    if downloaded.suffix.lower() == ".twb":
        return downloaded
    with zipfile.ZipFile(downloaded) as z:
        twbs = [i for i in z.infolist() if i.filename.lower().endswith(".twb")]
        if len(twbs) != 1:
            sys.exit(f"ERROR: {downloaded.name} contains {len(twbs)} .twb entries "
                     "(expected exactly 1); aborting before surgery")
        z.extract(twbs[0], dest_dir)
    return dest_dir / twbs[0].filename


def xml_attr(value: str) -> str:
    """Encode a value the way it appears inside a single-quoted TWB attribute.

    Tableau escapes double quotes as &quot; even inside single-quoted
    attributes (same rule the pds-augmenter's .tds writer follows). '&' must
    stay first so the other escapes are not double-escaped.
    """
    for raw, esc in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"),
                     ("'", "&apos;"), ('"', "&quot;")):
        value = value.replace(raw, esc)
    return value


_DS_OPEN_RE = re.compile(r"<datasource [^>]*caption='([^']*)'[^>]*>")
_REPO_ID_RE = re.compile(r"<repository-location [^>]*id='([^']*)'")
# A datasource's repository-location follows its open tag with at most a
# document-format-change-manifest in between; 3000 chars spans that
# comfortably. The window is additionally cut at the next datasource tag so a
# block without a repository-location can never borrow its neighbour's.
_REPO_WINDOW = 3000


def caption_repo_map(text: str) -> dict[str, str]:
    """caption -> repository-location id, one entry per real datasource block.

    Self-closing worksheet-level stubs (<datasource caption='X' ... />) are
    skipped — they have no repository-location child and would otherwise pair
    with unrelated elements further down. First match wins.
    """
    out: dict[str, str] = {}
    for m in _DS_OPEN_RE.finditer(text):
        if m.group(0).endswith("/>"):
            continue
        window = text[m.end(): m.end() + _REPO_WINDOW]
        for stop in ("<datasource ", "</datasource>"):
            idx = window.find(stop)
            if idx != -1:
                window = window[:idx]
        rm = _REPO_ID_RE.search(window)
        if rm:
            out.setdefault(m.group(1), rm.group(1))
    return out


def resolve_old_token(text: str, current_cu: str, old_name: str) -> tuple[str | None, str | None]:
    """Return (token the TWB actually references for this old PDS, warning).

    Prefer the PDS's current content_url; fall back to the repository-location
    id of the datasource captioned with the old display name (covers workbooks
    still referencing a stale, pre-republish content_url).
    """
    if f"id='{current_cu}'" in text or f"dbname='{current_cu}'" in text:
        return current_cu, None
    token = caption_repo_map(text).get(xml_attr(old_name))
    if token:
        return token, (f"workbook references stale content_url {token!r} for "
                       f"{old_name!r} (current is {current_cu!r}); using the TWB's token")
    return None, None


def apply_surgery(text: str, replacements: list[dict]) -> tuple[str, list[dict], list[str], list[str]]:
    """Attribute-scoped replacement. Returns (new_text, counts, errors, warnings).

    Exact `attr='value'` matches cannot cross-corrupt each other (no
    prefix-collision ordering needed) and keep content_url attrs and
    display-name attrs independent even when a PDS's display name equals its
    content_url. Stores the resolved token in each replacement as
    `old_token` for the post-publish staleness check.
    """
    counts: list[dict] = []
    errors: list[str] = []
    warnings: list[str] = []
    for r in replacements:
        token, warn = resolve_old_token(text, r["old_content_url"], r["old_name"])
        if token is None:
            errors.append(
                f"no reference to old PDS {r['old_name']!r} found in TWB (neither "
                f"current content_url {r['old_content_url']!r} nor a caption-matched "
                "repository-location) — stale design? re-run design mode")
            continue
        if warn:
            warnings.append(warn)
        r["old_token"] = token

        n_cu = 0
        for pat_old, pat_new in (
            (f"id='{token}'", f"id='{r['new_content_url']}'"),
            (f"dbname='{token}'", f"dbname='{r['new_content_url']}'"),
            (f"/datasources/{token}?", f"/datasources/{r['new_content_url']}?"),
        ):
            n_cu += text.count(pat_old)
            text = text.replace(pat_old, pat_new)
        n_name = 0
        for attr in ("caption", "server-ds-friendly-name"):
            pat_old = f"{attr}='{xml_attr(r['old_name'])}'"
            pat_new = f"{attr}='{xml_attr(r['new_name'])}'"
            n_name += text.count(pat_old)
            text = text.replace(pat_old, pat_new)
        counts.append({"old": r["old_name"], "new": r["new_name"], "token": token,
                       "content_url_attrs": n_cu, "name_attrs": n_name})
        if n_cu == 0:
            errors.append(f"token {token!r} resolved but no content_url attribute "
                          "was rewritten — unexpected TWB shape, aborting")
    for r in replacements:
        token = r.get("old_token")
        if not token:
            continue
        leftover = text.count(f"id='{token}'") + text.count(f"dbname='{token}'")
        if leftover:
            errors.append(f"{leftover} content_url attribute(s) for old token "
                          f"{token!r} survived surgery")
    return text, counts, errors, warnings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--design", required=True, help="repoint-design.json from design mode")
    ap.add_argument("--workbook", action="append", default=[], dest="workbooks",
                    help="workbook LUID to repoint (repeatable)")
    ap.add_argument("--all", action="store_true", help="repoint every workbook in the design")
    ap.add_argument("--stage", required=True, choices=("rehearsal", "production"))
    ap.add_argument("--rehearsal-project", default=None,
                    help="project name or LUID for rehearsal copies (required for --stage rehearsal)")
    ap.add_argument("--rehearsal-prefix", default=DEFAULT_REHEARSAL_PREFIX)
    ap.add_argument("--work-dir", required=True,
                    help="directory for downloaded originals and modified .twb (kept for audit)")
    ap.add_argument("--result-out", default=None,
                    help="also write the RESULT_JSON payload to this file "
                         "(render_rehearsal_report.py input)")
    args = ap.parse_args()

    if args.stage == "rehearsal" and not args.rehearsal_project:
        ap.error("--stage rehearsal requires --rehearsal-project")
    if bool(args.workbooks) == args.all:
        ap.error("select workbooks with either --workbook ... or --all (not both/neither)")

    t0 = time.monotonic()
    design = json.loads(Path(args.design).read_text(encoding="utf-8"))

    # wb_luid -> {name, replacements:[{old_luid,new_luid,old_name,new_name,new_content_url}]}
    plan: dict[str, dict] = {}
    luid_errors: list[str] = []
    # Workbooks touched by a pair that cannot be operated on (missing LUID).
    # Selecting any of them is a hard error — publishing would silently leave
    # that pair's old PDS reference in place (a partial repoint the connection
    # check cannot detect, since the pair never enters the replacement set).
    skipped_wbs: dict[str, list[str]] = {}
    for pr in design.get("pairs") or []:
        old, new = pr["old_pds"], pr["new_pds"]
        if not (old.get("luid") and new.get("luid")):
            luid_errors.append(
                f"pair {old.get('name')!r} -> {new.get('name')!r} lacks a LUID "
                f"(match={pr.get('match')}); run resolve-luids and rebuild the design")
            for wb in pr.get("workbooks") or []:
                skipped_wbs.setdefault(wb["luid"], []).append(old.get("name") or "?")
            continue
        for wb in pr.get("workbooks") or []:
            entry = plan.setdefault(wb["luid"], {"name": wb["name"], "replacements": []})
            entry["replacements"].append({
                "old_luid": old["luid"], "new_luid": new["luid"],
                "old_name": old["name"], "new_name": new["name"],
                "new_content_url": new.get("content_url") or "",
            })

    selected = sorted(set(plan) | set(skipped_wbs)) if args.all else args.workbooks
    missing = [w for w in selected if w not in plan and w not in skipped_wbs]
    if missing:
        sys.exit(f"ERROR: workbook LUID(s) not present in design pairs: {missing}")
    blocked = sorted(w for w in selected if w in skipped_wbs)
    if blocked:
        detail = "; ".join(f"{w} (old PDS: {', '.join(skipped_wbs[w])})" for w in blocked)
        sys.exit("ERROR: selected workbook(s) reference pairs without LUIDs — run "
                 "publish_manifest.py resolve-luids and rebuild the design. "
                 + detail + " | " + "; ".join(luid_errors))
    if not selected:
        sys.exit("ERROR: nothing to repoint"
                 + (" — " + "; ".join(luid_errors) if luid_errors else ""))

    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    with signed_in_server() as server:
        rehearsal_project_id = (
            resolve_project(server, args.rehearsal_project)
            if args.stage == "rehearsal" else None
        )

        # Resolve content_urls once per PDS LUID (design carries the new PDS
        # content_url as a hint; the old one is always resolved live).
        curl_cache: dict[str, str] = {}

        def content_url(luid: str) -> str:
            if luid not in curl_cache:
                curl_cache[luid] = server.datasources.get_by_id(luid).content_url
            return curl_cache[luid]

        for wb_luid in selected:
            entry = plan[wb_luid]
            reps = []
            for r in entry["replacements"]:
                reps.append({
                    "old_content_url": content_url(r["old_luid"]),
                    "new_content_url": r["new_content_url"] or content_url(r["new_luid"]),
                    "old_name": r["old_name"],
                    "new_name": r["new_name"],
                })

            orig = server.workbooks.get_by_id(wb_luid)
            dl_dir = work_dir / slug(orig.name)
            if dl_dir.exists():
                shutil.rmtree(dl_dir)
            dl_dir.mkdir(parents=True)
            downloaded = Path(server.workbooks.download(
                wb_luid, filepath=str(dl_dir), include_extract=False))
            twb_path = extract_twb(downloaded, dl_dir)

            text = twb_path.read_text(encoding="utf-8")
            new_text, counts, errors, wb_warnings = apply_surgery(text, reps)
            mod_path = dl_dir / f"{slug(orig.name)}_repointed.twb"
            mod_path.write_text(new_text, encoding="utf-8")

            if errors:
                results.append({
                    "workbook": orig.name, "workbook_luid": wb_luid,
                    "status": "surgery_failed", "errors": errors, "counts": counts,
                    "warnings": wb_warnings,
                })
                print(f"[repoint_workbook] SKIP publish for {orig.name!r}: "
                      + "; ".join(errors), file=sys.stderr)
                continue

            if args.stage == "rehearsal":
                item = TSC.WorkbookItem(
                    project_id=rehearsal_project_id,
                    name=f"{args.rehearsal_prefix}{orig.name}",
                    show_tabs=orig.show_tabs,
                )
            else:
                item = TSC.WorkbookItem(
                    project_id=orig.project_id, name=orig.name, show_tabs=orig.show_tabs,
                )
            published = server.workbooks.publish(
                item, str(mod_path), TSC.Server.PublishMode.Overwrite)

            server.workbooks.populate_connections(published)
            conn_names = set(c.datasource_name for c in published.connections)
            # Primary: nothing may still answer to an old display name or old
            # token. Secondary: each new PDS must show up as its display name
            # OR its content_url (REST datasource_name can return either).
            old_idents = ({r["old_name"] for r in reps}
                          | {r["old_token"] for r in reps if r.get("old_token")})
            stale = sorted(conn_names & old_idents)
            absent = sorted(
                r["new_name"] for r in reps
                if r["new_name"] not in conn_names
                and r["new_content_url"] not in conn_names
            )
            conn_ok = not stale and not absent

            results.append({
                "workbook": orig.name, "workbook_luid": wb_luid,
                "status": "ok" if conn_ok else "connection_check_failed",
                "original_webpage_url": orig.webpage_url,
                "published_luid": published.id,
                "published_name": published.name,
                "webpage_url": published.webpage_url,
                "luid_preserved": published.id == wb_luid,
                "counts": counts,
                "warnings": wb_warnings,
                "connections": sorted(conn_names),
                "stale_old_names": stale, "missing_new_names": absent,
                "modified_twb": str(mod_path).replace("\\", "/"),
            })
            print(f"[repoint_workbook] {args.stage}: {orig.name!r} -> "
                  f"{published.name!r} ({published.id}) conn_ok={conn_ok}",
                  file=sys.stderr)

    ok = sum(1 for r in results if r["status"] == "ok")
    payload = {
        "status": "ok" if ok == len(results) else "partial_failure",
        "stage": args.stage,
        "workbooks_ok": ok,
        "workbooks_failed": len(results) - ok,
        "results": results,
        "warnings": luid_errors,
        "elapsed_s": round(time.monotonic() - t0),
    }
    if args.result_out:
        out = Path(args.result_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("RESULT_JSON: " + json.dumps(payload, ensure_ascii=False))
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
