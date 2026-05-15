#!/usr/bin/env python3
"""Trigger a published Tableau Prep flow run on Server/Cloud.

Runs non-interactively. Approval is taken at session intake (CLAUDE.md step 0);
see references/autonomous-execution-policy.md.

Waits for completion by default (--no-wait to fire-and-forget). On completion,
emits a final line `RESULT_JSON: {...}` carrying structured status so an AI
agent driving the recovery loop can parse it without scraping human text.

Usage:
    python run_flow.py --flow-name "stg_orders" --project-name "Sales Analytics/stg"
    python run_flow.py --flow-id <luid>
    python run_flow.py --flow-id <luid> --no-wait
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))

import tableauserverclient as TSC  # noqa: E402

from tableau_auth import sign_in_server  # noqa: E402


# Tableau job finishCode meanings
FINISH_CODES = {0: "Success", 1: "Failed", 2: "Cancelled"}


def parse_args():
    p = argparse.ArgumentParser(description="Trigger a published Prep flow run")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--flow-name")
    grp.add_argument("--flow-id")
    p.add_argument("--project-name",
                   help="Disambiguate --flow-name (use leaf project name, e.g. 'stg')")
    p.add_argument("--wait", action=argparse.BooleanOptionalAction, default=True,
                   help="Block until the run finishes (default: True). "
                        "Use --no-wait to fire-and-forget.")
    p.add_argument("--poll-interval", type=int, default=30,
                   help="Polling interval in seconds (default: 30)")
    p.add_argument("--timeout", type=int, default=3600,
                   help="Max wait in seconds (default: 3600)")
    return p.parse_args()


def find_flow(server, *, flow_id, flow_name, project_name):
    if flow_id:
        return server.flows.get_by_id(flow_id)
    req = TSC.RequestOptions()
    req.filter.add(TSC.Filter(TSC.RequestOptions.Field.Name,
                              TSC.RequestOptions.Operator.Equals,
                              flow_name))
    flows, _ = server.flows.get(req)
    if project_name:
        leaf = project_name.split("/")[-1].strip()
        flows = [f for f in flows if f.project_name == leaf]
    if not flows:
        sys.exit(f"ERROR: No flow found matching name='{flow_name}'")
    if len(flows) > 1:
        sys.exit("ERROR: Multiple flows match — use --flow-id or add --project-name")
    return flows[0]


def emit_result(payload: dict) -> None:
    """Emit a single machine-readable line for the recovery-loop driver."""
    print(f"RESULT_JSON: {json.dumps(payload, ensure_ascii=False)}")


def main():
    args = parse_args()
    server, auth = sign_in_server()
    with server.auth.sign_in(auth):
        flow = find_flow(server,
                         flow_id=args.flow_id,
                         flow_name=args.flow_name,
                         project_name=args.project_name)

        job = server.flows.refresh(flow)
        print(f"Started flow run. Job id: {job.id}")

        if not args.wait:
            emit_result({
                "jobId": job.id,
                "flowId": flow.id,
                "flowName": flow.name,
                "status": "started",
                "finishCode": None,
                "notes": None,
                "durationSec": 0,
            })
            print(f"Use 'python get_job_status.py --job-id {job.id}' to check status.")
            return

        start = time.time()
        while time.time() - start < args.timeout:
            time.sleep(args.poll_interval)
            current = server.jobs.get_by_id(job.id)
            elapsed = int(time.time() - start)
            status = (FINISH_CODES.get(current.finish_code, "InProgress")
                      if current.completed_at is None
                      else FINISH_CODES.get(current.finish_code, f"Unknown({current.finish_code})"))
            print(f"  [{elapsed:>5}s] status={status}")
            if current.completed_at is not None:
                code = current.finish_code
                label = FINISH_CODES.get(code, f"Unknown({code})")
                notes = getattr(current, "notes", None)
                print(f"\nFinished: {label}")
                emit_result({
                    "jobId": job.id,
                    "flowId": flow.id,
                    "flowName": flow.name,
                    "status": label,
                    "finishCode": code,
                    "notes": notes,
                    "durationSec": elapsed,
                })
                sys.exit(0 if code == 0 else 1)
        emit_result({
            "jobId": job.id,
            "flowId": flow.id,
            "flowName": flow.name,
            "status": "Timeout",
            "finishCode": None,
            "notes": f"Timed out after {args.timeout}s",
            "durationSec": args.timeout,
        })
        sys.exit("Timeout waiting for job to finish.")


if __name__ == "__main__":
    main()
