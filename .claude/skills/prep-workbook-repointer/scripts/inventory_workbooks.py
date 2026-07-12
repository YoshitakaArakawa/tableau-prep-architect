#!/usr/bin/env python3
"""Read-only inventory of workbooks that consume the old (pre-migration) PDS.

design-mode Step 1. Answers the LEFT side of the repoint join:
"which workbooks reference which published datasource in the source project?"

Source of the answer is the Tableau Metadata API lineage field
`publishedDatasources { downstreamWorkbooks }` (confirmed to return real data;
the reverse direction `upstreamDatasources` is deliberately NOT used — see
references/lineage-model.md for the false-FAIL rationale). No demo/usage
filtering is applied: every workbook that reads a source-project PDS is listed,
per design decision 2 (the human decides what to keep).

For each target workbook, `webpage_url` (the embed/view URL a human clicks to
open it) is resolved from TSC's WorkbookItem. For each NEW marts PDS listed in
the caller's manifests, `content_url` is resolved from TSC's DatasourceItem and
carried forward as groundwork for the future .twb-surgery option (design.json
back-key); it is not used by the runbook or by verify.

Usage:
    python inventory_workbooks.py --source-project <source-project-name> \
        --manifest <publish-manifest_1.json> [--manifest <...> ...] \
        --out <output_dir>/repoint-inventory.json

Cloud access is read-only. Final line: RESULT_JSON: {...}
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# Import repo-common auth helper. From
# .claude/skills/prep-workbook-repointer/scripts/ the repo root is 4 parents up.
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))

import tableauserverclient as TSC  # noqa: E402

from tableau_auth import signed_in_server  # noqa: E402


def jst_now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds")


# The confirmed Metadata API lineage query. Non-connection list form (returns
# the full set, no cursor paging) matches the shape verified in prior sessions.
LINEAGE_QUERY = """
{
  publishedDatasources {
    luid
    name
    projectName
    downstreamWorkbooks { luid name projectName }
  }
}
"""


def query_lineage(server: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (published_datasources, errors) from the Metadata API.

    Each PDS dict: {luid, name, projectName, downstreamWorkbooks:[...]}.
    Errors are surfaced (not silently swallowed): an empty result with errors is
    the classic false-FAIL trap, so the caller must see them.
    """
    result = server.metadata.query(query=LINEAGE_QUERY)
    errors = [e.get("message", "?") for e in (result.get("errors") or [])]
    pds = (result.get("data") or {}).get("publishedDatasources") or []
    return pds, errors


def collect_new_pds_luids(manifest_paths: list[str]) -> dict[str, str]:
    """Map new-PDS luid -> new-PDS name from every decomposed flow output.

    The manifests are the source of truth for old->new correspondence; here we
    only need the set of NEW PDS luids so we can resolve their content_url.
    """
    new_by_luid: dict[str, str] = {}
    for mp in manifest_paths:
        m = json.loads(Path(mp).read_text(encoding="utf-8"))
        for df in m.get("decomposed_flows") or []:
            for o in df.get("outputs") or []:
                luid = o.get("luid")
                if luid:
                    new_by_luid[luid] = o.get("name") or ""
    return new_by_luid


def resolve_webpage_urls(server: Any, wb_luids: set[str]) -> dict[str, str]:
    """luid -> webpage_url for each target workbook (targeted get_by_id).

    Targeted lookups are O(targets), not O(site): a repoint set is typically
    dozens of workbooks, far fewer than a full site scan would page through.
    A workbook that cannot be fetched (deleted/permission) maps to "" and is
    reported as a warning by the caller rather than aborting the inventory.
    """
    urls: dict[str, str] = {}
    for luid in sorted(wb_luids):
        try:
            wb = server.workbooks.get_by_id(luid)
            urls[luid] = wb.webpage_url or ""
        except TSC.ServerResponseError as e:
            print(f"[inventory] WARNING: workbook {luid} not fetchable: {e}",
                  file=sys.stderr)
            urls[luid] = ""
    return urls


def resolve_content_urls(server: Any, ds_luids: dict[str, str]) -> dict[str, dict[str, str]]:
    """luid -> {name, content_url} for each new PDS (targeted get_by_id)."""
    index: dict[str, dict[str, str]] = {}
    for luid, name in sorted(ds_luids.items()):
        try:
            ds = server.datasources.get_by_id(luid)
            index[luid] = {"name": ds.name or name, "content_url": ds.content_url or ""}
        except TSC.ServerResponseError as e:
            print(f"[inventory] WARNING: datasource {luid} not fetchable: {e}",
                  file=sys.stderr)
            index[luid] = {"name": name, "content_url": ""}
    return index


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source-project", required=True,
                    help="Project name whose PDS are the repoint sources (site-specific; no default)")
    ap.add_argument("--manifest", action="append", default=[], dest="manifests",
                    help="publish-manifest.json path (repeatable) — used to resolve new-PDS content_url")
    ap.add_argument("--out", required=True, help="Write repoint-inventory.json here")
    args = ap.parse_args()

    t0 = time.monotonic()
    phase: dict[str, float] = {}

    with signed_in_server() as server:
        server_url = server.server_address
        site_name = server.site_url
        all_pds, meta_errors = query_lineage(server)
        phase["metadata_query"] = time.monotonic()

        # LEFT side: source-project PDS that actually feed at least one workbook.
        old_pds = [
            p for p in all_pds
            if p.get("projectName") == args.source_project
            and (p.get("downstreamWorkbooks") or [])
        ]

        wb_luids: set[str] = set()
        for p in old_pds:
            for wb in p.get("downstreamWorkbooks") or []:
                if wb.get("luid"):
                    wb_luids.add(wb["luid"])

        webpage_urls = resolve_webpage_urls(server, wb_luids)
        phase["resolve_webpage_urls"] = time.monotonic()

        new_by_luid = collect_new_pds_luids(args.manifests)
        new_pds_index = resolve_content_urls(server, new_by_luid)
        phase["resolve_content_urls"] = time.monotonic()

    warnings: list[str] = []
    if meta_errors:
        warnings.append(
            "Metadata API returned errors (results may be incomplete — do NOT "
            "treat an empty inventory as 'no workbooks affected'): "
            + "; ".join(meta_errors)
        )
    if not old_pds:
        warnings.append(
            f"No source-project PDS with downstream workbooks found in "
            f"'{args.source_project}'. Confirm the project name and that the "
            f"Metadata API is enabled on this site."
        )
    missing_url = [luid for luid, url in webpage_urls.items() if not url]
    if missing_url:
        warnings.append(
            f"{len(missing_url)} workbook(s) had no resolvable webpage_url "
            f"(deleted or permission-restricted): {', '.join(missing_url[:5])}"
            + (" ..." if len(missing_url) > 5 else "")
        )

    inventory = {
        "schema_version": "1",
        "generated_at": jst_now_iso(),
        "server": server_url,
        "site_name": site_name,
        "source_project": args.source_project,
        "old_pds": [
            {
                "luid": p.get("luid"),
                "name": p.get("name"),
                "project_name": p.get("projectName"),
                "downstream_workbooks": [
                    {
                        "luid": wb.get("luid"),
                        "name": wb.get("name"),
                        "project_name": wb.get("projectName"),
                        "webpage_url": webpage_urls.get(wb.get("luid"), ""),
                    }
                    for wb in (p.get("downstreamWorkbooks") or [])
                ],
            }
            for p in old_pds
        ],
        "new_pds_index": new_pds_index,
        "metadata_errors": meta_errors,
        "warnings": warnings,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")

    for w in warnings:
        print(f"[inventory] WARNING: {w}", file=sys.stderr)
    print(f"[inventory] wrote {out} "
          f"({len(old_pds)} old PDS, {len(wb_luids)} workbooks)", file=sys.stderr)

    end = time.monotonic()
    keys = ["metadata_query", "resolve_webpage_urls", "resolve_content_urls"]
    prev = t0
    breakdown = {}
    for k in keys:
        if k in phase:
            breakdown[k] = round(phase[k] - prev)
            prev = phase[k]
    print("RESULT_JSON: " + json.dumps({
        "status": "ok" if not meta_errors else "ok_with_metadata_errors",
        "old_pds": len(old_pds),
        "workbooks": len(wb_luids),
        "new_pds_resolved": len(new_pds_index),
        "out": str(out).replace("\\", "/"),
        "elapsed_s": round(end - t0),
        "breakdown": breakdown,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
