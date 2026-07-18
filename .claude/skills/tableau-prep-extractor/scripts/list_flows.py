#!/usr/bin/env python3
"""List all Tableau Prep flows accessible to the authenticated user.

Useful when you know the flow only by its numerical UI URL (e.g. .../flows/241407/...)
which is NOT the REST API LUID. This script prints LUID + project + name so you can
match against what you see in the Tableau Cloud UI.

Usage:
    python list_flows.py
    python list_flows.py --project "stg"          # filter by project name (leaf)
    python list_flows.py --name-contains "orders" # substring match on flow name
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))

from tableau_auth import signed_in_server  # noqa: E402


def parse_args():
    p = argparse.ArgumentParser(description="List Prep flows on Tableau Server/Cloud")
    p.add_argument("--project", help="Filter by project name (leaf project name only)")
    p.add_argument("--name-contains", help="Substring filter on flow name (case-insensitive)")
    p.add_argument("--url-contains",
                   help="Substring filter on webpage_url (e.g. '241407' to find a flow by its "
                        "Tableau Cloud UI numeric ID)")
    return p.parse_args()


def main():
    args = parse_args()
    with signed_in_server() as server:
        all_flows, _ = server.flows.get()

        flows = list(all_flows)
        if args.project:
            flows = [f for f in flows if f.project_name == args.project]
        if args.name_contains:
            needle = args.name_contains.lower()
            flows = [f for f in flows if needle in (f.name or "").lower()]
        if args.url_contains:
            flows = [f for f in flows if args.url_contains in (f.webpage_url or "")]

        print(f"Total matched flows: {len(flows)}")
        print()
        for f in sorted(flows, key=lambda x: (x.project_name or "", x.name or "")):
            print(f"LUID:    {f.id}")
            print(f"  name:    {f.name}")
            print(f"  project: {f.project_name or '-'}")
            print(f"  url:     {f.webpage_url}")
            print()


if __name__ == "__main__":
    main()
