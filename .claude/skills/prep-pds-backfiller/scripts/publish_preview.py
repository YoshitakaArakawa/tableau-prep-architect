#!/usr/bin/env python3
"""Publish backfilled .tdsx extracts to a scratch project for GUI review.

This does NOT touch production PDS. It publishes each <tag>_backfilled.tdsx
(produced by backfill_pds.py dry-run) as a SEPARATE datasource into a throwaway
child project, so the backfilled data can be opened / eyeballed in Tableau
before the real Overwrite. Non-destructive: production PDS are untouched.
Idempotent: Overwrite within the scratch child project only (matches by name in
THAT project).

The parent (sandbox) project is given by LUID or by path -- never hardcoded.
Preview datasource names are resolved from the server by each entry's new_luid
so the preview mirrors the production name (suffixed to avoid confusion).

Usage:
  python publish_preview.py --spec backfill-spec.json --workdir <dir> \
      --parent-path <sandbox-path> --project backfill_preview_20260712
  python publish_preview.py --spec backfill-spec.json --workdir <dir> \
      --parent-luid <luid> --project backfill_preview --only f02
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "scripts"))

import tableauserverclient as TSC  # noqa: E402
from tableau_auth import signed_in_server  # noqa: E402

PREVIEW_SUFFIX = "__backfill_preview"


def resolve_path_to_luid(server, path: str) -> str:
    """Walk a '/'-separated project path from the top and return the leaf LUID."""
    segments = [s for s in path.strip("/").split("/") if s]
    all_projects = list(TSC.Pager(server.projects))
    parent_id = None
    luid = None
    for seg in segments:
        match = [p for p in all_projects
                 if p.name == seg and p.parent_id == parent_id]
        if not match:
            sys.exit(f"ERROR: project segment {seg!r} not found under parent "
                     f"{parent_id!r} (path {path!r})")
        if len(match) > 1:
            sys.exit(f"ERROR: ambiguous project segment {seg!r} under {parent_id!r}")
        luid = match[0].id
        parent_id = luid
    return luid


def find_child(server, parent_id: str, name: str):
    for p in TSC.Pager(server.projects):
        if p.parent_id == parent_id and p.name == name:
            return p
    return None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--spec", required=True, help="backfill spec JSON (for tags)")
    ap.add_argument("--workdir", required=True,
                    help="dir holding <tag>_backfilled.tdsx (backfill_pds.py output)")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--parent-luid", help="sandbox parent project LUID")
    group.add_argument("--parent-path", help="sandbox parent project path (e.g. Sandbox)")
    ap.add_argument("--project", required=True, help="throwaway child project name")
    ap.add_argument("--only", help="one tag; default all in the spec")
    args = ap.parse_args(argv)

    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    entries = {e["tag"]: e for e in spec["flows"]}
    tags = [args.only] if args.only else list(entries)
    workdir = Path(args.workdir)

    for t in tags:
        if t not in entries:
            sys.exit(f"unknown tag {t!r}; choices: {list(entries)}")
        tdsx = workdir / f"{t}_backfilled.tdsx"
        if not tdsx.exists():
            sys.exit(f"missing {tdsx} -- run backfill_pds.py --only {t} first")

    results = []
    with signed_in_server() as server:
        parent_id = args.parent_luid or resolve_path_to_luid(server, args.parent_path)
        proj = find_child(server, parent_id, args.project)
        if proj is None:
            proj = server.projects.create(
                TSC.ProjectItem(name=args.project, parent_id=parent_id))
            print(f"[project] created {args.project!r} luid={proj.id}")
        else:
            print(f"[project] reuse {args.project!r} luid={proj.id}")

        for t in tags:
            tdsx = workdir / f"{t}_backfilled.tdsx"
            prod_name = server.datasources.get_by_id(entries[t]["new_luid"]).name
            preview_name = f"{prod_name}{PREVIEW_SUFFIX}"
            ds_item = TSC.DatasourceItem(project_id=proj.id, name=preview_name)
            pub = server.datasources.publish(
                ds_item, str(tdsx), mode=TSC.Server.PublishMode.Overwrite)
            print(f"[{t}] published {preview_name!r} luid={pub.id}")
            results.append({"tag": t, "preview_name": preview_name, "luid": pub.id})

    print("\n=== open in Tableau ===")
    print(f"project path leaf: {args.project}")
    for r in results:
        print(f"  {r['tag']}: {r['preview_name']}  (luid {r['luid']})")
    print("RESULT_JSON: " + json.dumps({
        "status": "ok", "project": args.project, "previews": results,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
