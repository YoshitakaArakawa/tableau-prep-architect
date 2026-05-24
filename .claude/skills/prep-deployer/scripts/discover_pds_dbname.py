#!/usr/bin/env python3
"""Resolve the physical Hyper name (dbname) of a Published Data Source on Tableau Cloud.

prep-builder generates downstream .tfl files with `LoadSqlProxy` Inputs pointing
at upstream layer PDSes. At build time the physical Hyper name is unknown
(Cloud assigns `<datasourceName>_<17-digit-suffix>` when the PDS is first
published). After the upstream layer's publish + run completes, this script
queries the Metadata API and patches the downstream .tfl(s) via
`flow_io.patch_pds_dbname`.

For the common case (patch every downstream .tfl using session-manifest run
state in one shot), use `auto_patch_downstream.py` instead — it covers
multiple (PDS x .tfl) pairs per invocation and is idempotent. Use this
script when you want to inspect a single PDS's dbname candidates or patch a
single .tfl in isolation (debug / one-off).

Usage:

    python discover_pds_dbname.py --datasource-name stg_transactions \\
                                  --project-path "99_Sandbox/.../stg"

    # patch a downstream .tfl with the discovered dbname
    python discover_pds_dbname.py --datasource-name stg_transactions \\
                                  --project-path "99_Sandbox/.../stg" \\
                                  --patch ./flows/intermediate/int_joined.tfl

⚠️ Empirical status (20260520): the exact field name returned by the Metadata API
that maps to the LoadSqlProxy `connectionAttributes.dbname` is not yet
confirmed. The candidates observed in the wild are:
  - Metadata API `Database.name` on the embedded extract database
  - `databaseTable.fullName` minus schema prefix
  - The auto-generated suffix appended to `datasourceName`
This script tries them in order and prints all of them so the operator can pick.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))

import tableauserverclient as TSC  # noqa: E402

from flow_io import load_aux_entries, load_flow_json, pack_flow_json, patch_pds_dbname  # noqa: E402
from tableau_auth import signed_in_server  # noqa: E402


METADATA_QUERY = """
query findEmbeddedExtract($name: String!, $projectName: String!) {
  publishedDatasources(filter: {name: $name, projectName: $projectName}) {
    luid
    name
    projectName
    hasExtracts
    extractLastRefreshTime
    upstreamDatabases { name connectionType }
  }
}
""".strip()


def fetch_all_projects(server) -> list:
    """Fetch every project on the site once (used as a cache by callers)."""
    all_projects, _ = server.projects.get(req_options=TSC.RequestOptions(pagesize=1000))
    return all_projects


def resolve_project_id(server, project_path: str, *, projects_cache: list | None = None) -> str:
    parts = [seg.strip() for seg in project_path.split("/") if seg.strip()]
    all_projects = projects_cache if projects_cache is not None else fetch_all_projects(server)
    parent_id = None
    for name in parts:
        match = [p for p in all_projects if p.name == name and p.parent_id == parent_id]
        if not match:
            sys.exit(f"ERROR: project segment '{name}' not found (parent_id={parent_id})")
        parent_id = match[0].id
    return parent_id


def discover(server, *, datasource_name: str, project_path: str,
             projects_cache: list | None = None) -> dict:
    project_id = resolve_project_id(server, project_path, projects_cache=projects_cache)
    # Filter by name+project_id
    req = TSC.RequestOptions()
    req.filter.add(
        TSC.Filter(TSC.RequestOptions.Field.Name,
                   TSC.RequestOptions.Operator.Equals, datasource_name)
    )
    matched = []
    for ds in TSC.Pager(server.datasources, req):
        if ds.project_id == project_id:
            matched.append(ds)
    if not matched:
        sys.exit(f"ERROR: no datasource '{datasource_name}' in '{project_path}'")
    if len(matched) > 1:
        sys.exit(f"ERROR: multiple matches for '{datasource_name}' in '{project_path}'")

    ds = matched[0]
    server.datasources.populate_connections(ds)

    result = {
        "datasource_name": datasource_name,
        "project_path": project_path,
        "luid": ds.id,
        "content_url": ds.content_url,
        "candidates": {
            "content_url": ds.content_url,
            "name_only": datasource_name,
            "connections_dbnames": [
                getattr(c, "datasource_name", None) for c in (ds.connections or [])
            ],
        },
    }

    # Try Metadata API for the most authoritative answer
    try:
        meta = server.metadata.query(
            METADATA_QUERY,
            variables={"name": datasource_name, "projectName": project_path.rsplit("/", 1)[-1]},
        )
        result["candidates"]["metadata_api"] = meta
    except Exception as e:  # broad: Metadata API may be disabled on the site
        result["candidates"]["metadata_api_error"] = f"{type(e).__name__}: {e}"

    return result


def patch_file(tfl_path: Path, *, datasource_name: str, project_path: str, dbname: str) -> int:
    flow = load_flow_json(tfl_path)
    aux = load_aux_entries(tfl_path)
    n = patch_pds_dbname(
        flow,
        datasource_name=datasource_name,
        project_name=project_path,
        dbname=dbname,
    )
    pack_flow_json(flow, tfl_path, aux_entries=aux)
    return n


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--datasource-name", required=True)
    p.add_argument("--project-path", required=True,
                   help="full project path on Cloud, slash-separated")
    p.add_argument("--patch", action="append", default=[],
                   help="downstream .tfl(s) to patch (LoadSqlProxy dbname)")
    p.add_argument("--use-candidate", default="content_url",
                   help="which candidate to use as dbname when --patch is given "
                        "(default: content_url)")
    args = p.parse_args()

    with signed_in_server() as server:
        result = discover(server, datasource_name=args.datasource_name,
                          project_path=args.project_path)
        print(json.dumps(result, indent=2, ensure_ascii=False))

        if args.patch:
            dbname = result["candidates"].get(args.use_candidate)
            if not dbname:
                sys.exit(f"ERROR: candidate '{args.use_candidate}' empty in result")
            for path in args.patch:
                n = patch_file(Path(path),
                               datasource_name=args.datasource_name,
                               project_path=args.project_path,
                               dbname=dbname)
                print(f"[patch] {path}: updated {n} LoadSqlProxy node(s) "
                      f"with dbname={dbname}")


if __name__ == "__main__":
    main()
