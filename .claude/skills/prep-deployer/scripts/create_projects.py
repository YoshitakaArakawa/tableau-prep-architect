#!/usr/bin/env python3
"""Create stg/intermediate/marts subprojects under a user-specified parent project.

Idempotent: if a subproject already exists with the same name + parent, skip it.

Usage:
    python create_projects.py --parent-name "Sales Analytics"
    python create_projects.py --parent-id <luid>
    python create_projects.py --parent-name "Sales Analytics" --layers stg,marts
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))

import tableauserverclient as TSC  # noqa: E402

from tableau_auth import signed_in_server  # noqa: E402


DEFAULT_LAYERS = [
    ("stg",          "Staging layer — 1 source per .tfl, no joins. dbt sources equivalent."),
    ("intermediate", "Intermediate layer — joins, business logic, pre-aggregations. Not for direct publish."),
    ("marts",        "Marts layer — fct_/dim_ separated, plus rpt_ for joined Published DS consumed by Workbooks."),
]


def parse_args():
    p = argparse.ArgumentParser(description="Create dbt-style layer subprojects on Tableau Server/Cloud")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--parent-name", help="Top-level parent project name")
    grp.add_argument("--parent-id", help="Parent project LUID")
    p.add_argument("--layers", default="stg,intermediate,marts",
                   help="Comma-separated subset of layers to create (default: all 3)")
    return p.parse_args()


def find_top_level_project(server, *, project_id, project_name):
    if project_id:
        return server.projects.get_by_id(project_id)
    req = TSC.RequestOptions()
    req.filter.add(TSC.Filter(TSC.RequestOptions.Field.Name,
                              TSC.RequestOptions.Operator.Equals,
                              project_name))
    projects, _ = server.projects.get(req)
    top = [p for p in projects if p.parent_id is None]
    if not top:
        sys.exit(f"ERROR: No top-level project named '{project_name}'")
    if len(top) > 1:
        sys.exit(f"ERROR: Multiple top-level projects named '{project_name}'. Use --parent-id.")
    return top[0]


def main():
    args = parse_args()
    requested = {n.strip() for n in args.layers.split(",")}
    targets = [(n, d) for (n, d) in DEFAULT_LAYERS if n in requested]
    unknown = requested - {n for (n, _) in DEFAULT_LAYERS}
    if unknown:
        sys.exit(f"ERROR: Unknown layer(s): {unknown}. Available: {[n for n,_ in DEFAULT_LAYERS]}")
    if not targets:
        sys.exit("ERROR: No valid layers requested.")

    with signed_in_server() as server:
        parent = find_top_level_project(server,
                                        project_id=args.parent_id,
                                        project_name=args.parent_name)
        print(f"Parent project: {parent.name} (id={parent.id})")

        # List existing children
        all_projects, _ = server.projects.get()
        existing_under_parent = {p.name: p for p in all_projects if p.parent_id == parent.id}

        for name, description in targets:
            if name in existing_under_parent:
                print(f"  [skip]    '{name}' already exists (id={existing_under_parent[name].id})")
                continue
            new_proj = TSC.ProjectItem(
                name=name,
                description=description,
                content_permissions=TSC.ProjectItem.ContentPermissions.ManagedByOwner,
                parent_id=parent.id,
            )
            created = server.projects.create(new_proj)
            print(f"  [created] '{created.name}' (id={created.id})")


if __name__ == "__main__":
    main()
