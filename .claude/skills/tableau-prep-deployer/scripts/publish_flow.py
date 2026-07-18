#!/usr/bin/env python3
"""Publish a .tfl/.tflx to a target project on Tableau Server/Cloud.

Runs non-interactively. Approval is taken at session intake (migration-workflow step 0
"Q2a goal" + "Q4 target path"); see references/autonomous-recovery.md.

Usage:
    python publish_flow.py --file ./flows/staging/stg_orders.tfl \\
                           --project-path "Sales Analytics/stg"
    python publish_flow.py --file ./flows/marts/fct_sales.tflx \\
                           --project-id <luid> --mode Overwrite

NOTE: Assumes inputs are Published DS / virtual connection (no embedded
credentials needed). Raw DB connections require `connections` parameter
wiring that this skeleton does not implement.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))

import tableauserverclient as TSC  # noqa: E402

from tableau_auth import signed_in_server  # noqa: E402


def parse_args():
    p = argparse.ArgumentParser(description="Publish a Prep flow to Tableau Server/Cloud")
    p.add_argument("--file", required=True, help="Path to .tfl/.tflx")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--project-id", help="Target project LUID")
    grp.add_argument("--project-path",
                     help="Project path like 'Parent/Child' (slash-separated)")
    p.add_argument("--name", help="Override flow name (default: filename stem)")
    p.add_argument("--mode", choices=["CreateNew", "Overwrite"], default="CreateNew")
    return p.parse_args()


def resolve_project_id(server, *, project_id, project_path):
    if project_id:
        return project_id
    parts = [seg.strip() for seg in project_path.split("/") if seg.strip()]
    if not parts:
        sys.exit("ERROR: --project-path must not be empty")

    all_projects, _ = server.projects.get()
    parent_id = None
    for name in parts:
        match = [p for p in all_projects if p.name == name and p.parent_id == parent_id]
        if not match:
            sys.exit(f"ERROR: Project segment '{name}' not found "
                     f"(parent_id={parent_id})")
        if len(match) > 1:
            sys.exit(f"ERROR: Ambiguous project name '{name}' at this level")
        parent_id = match[0].id
    return parent_id


def main():
    args = parse_args()
    file_path = Path(args.file).resolve()
    if not file_path.exists():
        sys.exit(f"ERROR: File not found: {file_path}")

    with signed_in_server() as server:
        project_id = resolve_project_id(server,
                                        project_id=args.project_id,
                                        project_path=args.project_path)

        flow_item = TSC.FlowItem(project_id=project_id,
                                 name=args.name or file_path.stem)
        published = server.flows.publish(
            flow_item,
            str(file_path),
            mode=getattr(TSC.Server.PublishMode, args.mode),
        )
        print(f"Published flow '{published.name}' (id={published.id}) "
              f"to project_id={project_id}")


if __name__ == "__main__":
    main()
