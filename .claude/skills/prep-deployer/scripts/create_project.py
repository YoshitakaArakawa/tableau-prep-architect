#!/usr/bin/env python3
"""Create ONE project as a child of an existing project (or at top-level), idempotent.

Building block used by prep-deployer's preflight to materialize each segment of
the deploy-context.md `pending_segments` list. The caller picks the parent
(typically the deepest existing prefix or the previous segment just created)
and supplies the new project name.

Runs non-interactively. Approval for the whole target path is taken at session
intake (migration-workflow step 0 Q4); see references/autonomous-recovery.md.
Top-level creation (no --parent-id / --parent-path) is still allowed but emits
a WARNING line on stderr for governance visibility.

Idempotent: if a project with the same name already exists under the parent,
prints its LUID and exits 0 without re-creating.

Usage:
    python create_project.py --parent-path "99_Sandbox" \\
                              --name "flow241407_decompose"

    python create_project.py --parent-id <luid> --name "Q4-2026"

    python create_project.py --name "new-top-level-project"   # top-level
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))

import tableauserverclient as TSC  # noqa: E402

from tableau_auth import signed_in_server  # noqa: E402


def parse_args():
    p = argparse.ArgumentParser(
        description="Create a single project under an existing parent (idempotent)")
    grp = p.add_mutually_exclusive_group(required=False)
    grp.add_argument("--parent-path",
                     help='Parent project name or "A/B/C" path (must exist). '
                          'Omit both --parent-path and --parent-id to create at top-level.')
    grp.add_argument("--parent-id", help="Parent project LUID")
    p.add_argument("--name", required=True,
                   help="Name of the new project to create")
    p.add_argument("--description", default="",
                   help="Optional description for the new project")
    return p.parse_args()


def resolve_parent_by_path(projects, path: str):
    segments = [s.strip() for s in path.split("/") if s.strip()]
    if not segments:
        sys.exit("ERROR: empty --parent-path")

    parent_id = None
    cur = None
    for i, seg in enumerate(segments):
        candidates = [p for p in projects
                      if p.parent_id == parent_id and p.name == seg]
        if not candidates:
            traversed = "/".join(segments[:i])
            sys.exit(
                f"ERROR: no child '{seg}' under "
                f"'{traversed if traversed else '<top-level>'}' "
                "(parent must exist; this script creates only ONE level)")
        if len(candidates) > 1:
            sys.exit(f"ERROR: ambiguous segment '{seg}' at path position {i + 1}")
        cur = candidates[0]
        parent_id = cur.id
    return cur


def main():
    args = parse_args()

    with signed_in_server() as server:
        all_projects, _ = server.projects.get(
            TSC.RequestOptions(pagesize=1000))
        all_projects = list(all_projects)

        parent = None
        if args.parent_id:
            try:
                parent = server.projects.get_by_id(args.parent_id)
            except Exception as e:
                sys.exit(f"ERROR: cannot fetch parent '{args.parent_id}': {e}")
        elif args.parent_path:
            parent = resolve_parent_by_path(all_projects, args.parent_path)

        parent_id = parent.id if parent else None
        existing = [p for p in all_projects
                    if p.parent_id == parent_id and p.name == args.name]
        if existing:
            wf = existing[0]
            parent_label = f"under '{parent.name}'" if parent else "at top-level"
            print(f"[skip] '{args.name}' already exists {parent_label}")
            print(f"  LUID: {wf.id}")
            return

        if not parent:
            sys.stderr.write(
                f"WARNING: creating top-level project '{args.name}' — "
                "org governance implications. Audit after the fact.\n"
            )

        new_proj = TSC.ProjectItem(
            name=args.name,
            description=args.description,
            content_permissions=TSC.ProjectItem.ContentPermissions.ManagedByOwner,
            parent_id=parent_id,
        )
        created = server.projects.create(new_proj)
        parent_label = f"under '{parent.name}'" if parent else "at top-level"
        print(f"[created] '{created.name}' {parent_label}")
        print(f"  LUID: {created.id}")


if __name__ == "__main__":
    main()
