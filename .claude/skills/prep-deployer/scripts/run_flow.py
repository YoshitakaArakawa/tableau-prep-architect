#!/usr/bin/env python3
"""Trigger a published Tableau Prep flow run on Server/Cloud.

Runs non-interactively. Approval is taken at session intake (CLAUDE.md step 0);
see references/autonomous-recovery.md.

Waits for completion by default (--no-wait to fire-and-forget). On completion,
emits a final line `RESULT_JSON: {...}` carrying structured status so an AI
agent driving the recovery loop can parse it without scraping human text.

Run mode:
    Default is a FULL run (replaces the output extract). Pass --incremental for
    an incremental run (reads only source rows past the control field's
    high-water mark and APPENDS). Incremental only makes sense for flows built
    with incremental refresh + append output (flow_io.set_incremental_refresh) -
    on a full/replace flow it behaves like a full run. Conversely, running an
    APPEND flow in full mode duplicates its output, so after the initial
    baseline full run, always use --incremental for append flows.

Usage:
    python run_flow.py --flow-name "stg_orders" --project-name "Sales Analytics/stg"
    python run_flow.py --flow-id <luid>
    python run_flow.py --flow-id <luid> --no-wait
    python run_flow.py --flow-id <luid> --incremental
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))

import tableauserverclient as TSC  # noqa: E402

from tableau_auth import signed_in_server  # noqa: E402


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
    p.add_argument("--incremental", action="store_true",
                   help="Run in incremental mode (append only new source rows). "
                        "Required for append flows after their baseline full run; "
                        "a full run of an append flow duplicates its output.")
    return p.parse_args()


def start_flow_run(server, flow, *, incremental: bool):
    """Trigger a flow run. TSC's flows.refresh() posts an empty body = FULL run;
    for incremental we hand-roll the /run POST with a flowRunSpec runMode."""
    if not incremental:
        return server.flows.refresh(flow)
    # runMode="incremental" needs flowId inside the spec, else Tableau 404s on
    # flow 'null'. Verified against REST API 3.x /flows/<id>/run.
    url = f"{server.flows.baseurl}/{flow.id}/run"
    body = (f'<tsRequest><flowRunSpec flowId="{flow.id}" '
            f'runMode="incremental"/></tsRequest>').encode("utf-8")
    resp = server.flows.post_request(url, body)
    return TSC.JobItem.from_response(resp.content, server.namespace)[0]


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
    with signed_in_server() as server:
        flow = find_flow(server,
                         flow_id=args.flow_id,
                         flow_name=args.flow_name,
                         project_name=args.project_name)

        job = start_flow_run(server, flow, incremental=args.incremental)
        mode = "incremental" if args.incremental else "full"
        print(f"Started flow run ({mode}). Job id: {job.id}")

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
        last_status = None
        while time.time() - start < args.timeout:
            time.sleep(args.poll_interval)
            current = server.jobs.get_by_id(job.id)
            elapsed = int(time.time() - start)
            status = (FINISH_CODES.get(current.finish_code, "InProgress")
                      if current.completed_at is None
                      else FINISH_CODES.get(current.finish_code, f"Unknown({current.finish_code})"))
            # Print on status TRANSITIONS only — per-interval lines add no
            # information and pile up in the driving agent's context.
            if status != last_status:
                print(f"  [{elapsed:>5}s] status={status}")
                last_status = status
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
