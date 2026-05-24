#!/usr/bin/env python3
"""Download a Tableau Prep Flow (.tfl/.tflx) from Tableau Server/Cloud.

Authentication is OAuth 2.0 (PKCE) via browser sign-in. .env in the working
directory or any ancestor must define SERVER and SITE_NAME.

Usage:
    python download_flow.py --flow-name "My Flow" --output ./flows/legacy.tflx
    python download_flow.py --flow-id 12345-abcde --output ./flows/legacy.tflx
    python download_flow.py --flow-name "stg_orders" --project-name "Sales Analytics/stg" \\
                            --output ./flows/staging/stg_orders.tflx
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))

import tableauserverclient as TSC  # noqa: E402

from tableau_auth import signed_in_server  # noqa: E402


def parse_args():
    p = argparse.ArgumentParser(description="Download a Tableau Prep Flow from Server/Cloud")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--flow-name", help="Flow name (case-sensitive)")
    grp.add_argument("--flow-id", help="Flow LUID")
    p.add_argument("--output", required=True, help="Output file path (.tfl or .tflx)")
    p.add_argument("--project-name",
                   help="Optional: scope by project name when --flow-name is ambiguous "
                        "(use 'Parent/Child' for nested projects)")
    return p.parse_args()


def find_flow(server, *, flow_id, flow_name, project_name):
    if flow_id:
        return server.flows.get_by_id(flow_id)

    req = TSC.RequestOptions()
    req.filter.add(TSC.Filter(TSC.RequestOptions.Field.Name,
                              TSC.RequestOptions.Operator.Equals,
                              flow_name))
    all_flows, _ = server.flows.get(req)

    if project_name:
        target_leaf = project_name.split("/")[-1].strip()
        all_flows = [f for f in all_flows if f.project_name == target_leaf]

    if not all_flows:
        sys.exit(f"ERROR: No flow found matching name='{flow_name}'"
                 + (f" in project='{project_name}'" if project_name else ""))
    if len(all_flows) > 1:
        sys.exit(f"ERROR: Multiple flows match name='{flow_name}'. "
                 "Disambiguate with --project-name or use --flow-id.")
    return all_flows[0]


def main():
    args = parse_args()
    with signed_in_server() as server:
        flow = find_flow(server,
                         flow_id=args.flow_id,
                         flow_name=args.flow_name,
                         project_name=args.project_name)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        file_path = server.flows.download(flow.id, filepath=str(output))
        print(f"Downloaded flow '{flow.name}' (id={flow.id}) → {file_path}")


if __name__ == "__main__":
    main()
