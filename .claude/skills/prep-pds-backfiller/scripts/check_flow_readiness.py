#!/usr/bin/env python3
"""Read-only interlock gate: is the accumulator flow safe to Overwrite right now?

A backfill Overwrite must not race a concurrent run of the same flow:
  (a) a scheduled run in flight can grab the OLD extract and revert the Overwrite;
  (b) a run firing just after the Overwrite re-appends and duplicates.

Scheduling on Tableau Cloud cannot be mutated over REST -- Linked Tasks are
UI-only (create/suspend/delete). So this script does NOT suspend anything; it
REPORTS what would collide, and the recipe routes the operator to suspend the
schedule in the Cloud UI before --execute (a manual gate, then re-run this to
confirm).

An active schedule is only a HARD blocker if it could fire within the operation
window (--window-minutes); one whose next run is comfortably beyond the window is
advisory, not blocking -- suspending it is then over-cautious, because a seam
backfill preserves the watermark and a later incremental scheduled run does not
duplicate. So this reports next-run timing, not just Active/Suspended.

Reports for one flow LUID:
  - active_schedules   : runFlow tasks + Linked Task steps targeting this flow,
                         each annotated with minutes_to_next_run; those within
                         the window block, those beyond it are advisory
  - suspended_schedules: same, already Suspended (informational)
  - running_flow_jobs  : RunFlow jobs currently Pending/InProgress. Job list
                         entries do not carry the flow LUID, so matching is by
                         flow-name substring in the job title/subtitle (a hint,
                         not proof) -- any active RunFlow job is surfaced.

Usage:
  python check_flow_readiness.py --flow-luid <accumulator-flow-luid>

Cloud access is read-only. Final line: RESULT_JSON: {...}
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "scripts"))

from tableau_auth import USER_AGENT, signed_in_server  # noqa: E402

# Background-job statuses that mean "not finished yet".
ACTIVE_JOB_STATES = {"Pending", "InProgress"}
# Default operation window (minutes): an active schedule is only a hard blocker
# if it could fire within this many minutes of now. Overwrite + acceptance run
# take a few minutes, so 60 leaves ample margin. An active schedule whose next
# run is beyond the window is advisory, not blocking -- suspending it is then
# over-cautious (a scheduled *incremental* run after a seam backfill preserves
# the watermark and does not duplicate).
DEFAULT_WINDOW_MINUTES = 60


def _get_json(server, path: str) -> dict:
    url = f"{server.server_address}/api/{server.version}/sites/{server.site_id}/{path}"
    req = urllib.request.Request(url, method="GET", headers={
        "Accept": "application/json",
        "X-Tableau-Auth": server.auth_token,
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode(errors="replace")[:500]}


def schedules_for_flow(server, flow_luid: str) -> tuple[list, list]:
    """Return (active, suspended) schedule views targeting this flow, across
    standalone runFlow tasks and Linked Task steps."""
    active, suspended = [], []

    def record(state, view):
        (active if state == "Active" else suspended).append(view)

    rf = _get_json(server, "tasks/runFlow")
    if "_http_error" not in rf:
        for t in (rf.get("tasks") or {}).get("task", []):
            fr = t.get("flowRun") or {}
            if (fr.get("flow") or {}).get("id") != flow_luid:
                continue
            sch = fr.get("schedule") or {}
            record(sch.get("state"), {
                "kind": "runFlow", "task_id": fr.get("id"),
                "state": sch.get("state"), "frequency": sch.get("frequency"),
                "next_run_at": sch.get("nextRunAt"),
            })

    lt = _get_json(server, "tasks/linked")
    if "_http_error" not in lt:
        for task in (lt.get("linkedTasks") or {}).get("linkedTasks", []):
            steps = (task.get("linkedTaskSteps") or {}).get("linkedTaskSteps") or []
            for st in steps:
                fr = ((st.get("task") or {}).get("flowRun")) or {}
                if (fr.get("flow") or {}).get("id") != flow_luid:
                    continue
                sch = task.get("schedule") or {}
                record(sch.get("state"), {
                    "kind": "linkedTask", "linked_task_id": task.get("id"),
                    "step_number": st.get("stepNumber"),
                    "state": sch.get("state"), "frequency": sch.get("frequency"),
                    "next_run_at": sch.get("nextRunAt"),
                })
    return active, suspended


def _minutes_until(iso_ts: str | None) -> float | None:
    """Minutes from now (UTC) until an ISO-8601 timestamp; None if unparseable."""
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt - datetime.now(timezone.utc)).total_seconds() / 60.0
    except ValueError:
        return None


def running_flow_jobs(server, flow_name: str | None) -> list:
    """Best-effort: RunFlow jobs still Pending/InProgress. The job list does not
    expose the flow LUID, so name-match is a hint (title/subtitle substring)."""
    data = _get_json(server, "jobs")
    if "_http_error" in data:
        return [{"_warning": f"GET jobs failed: {data['_http_error']}"}]
    out = []
    for j in (data.get("backgroundJobs") or {}).get("backgroundJob", []):
        if j.get("jobType") != "RunFlow" or j.get("status") not in ACTIVE_JOB_STATES:
            continue
        blob = f"{j.get('title', '')} {j.get('subtitle', '')}"
        out.append({
            "job_id": j.get("id"), "status": j.get("status"),
            "created_at": j.get("createdAt"), "subtitle": j.get("subtitle"),
            "name_match": bool(flow_name and flow_name in blob),
        })
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--flow-luid", required=True, help="accumulator flow LUID")
    ap.add_argument("--window-minutes", type=int, default=DEFAULT_WINDOW_MINUTES,
                    help="an active schedule is a hard blocker only if its next run "
                         f"is within this many minutes (default {DEFAULT_WINDOW_MINUTES})")
    args = ap.parse_args(argv)

    with signed_in_server() as server:
        try:
            flow = server.flows.get_by_id(args.flow_luid)
            flow_name = flow.name
        except Exception:
            flow_name = None
        active, suspended = schedules_for_flow(server, args.flow_luid)
        jobs = running_flow_jobs(server, flow_name)

    # Split active schedules by whether they could fire within the operation
    # window. An unknown next-run time is treated as imminent (conservative).
    window = args.window_minutes
    imminent, distant = [], []
    for sch in active:
        mins = _minutes_until(sch.get("next_run_at"))
        sch["minutes_to_next_run"] = round(mins) if mins is not None else None
        (imminent if mins is None or mins <= window else distant).append(sch)

    name_matched_running = any(j.get("name_match") for j in jobs)
    blockers, advisories = [], []
    if imminent:
        blockers.append(f"{len(imminent)} active schedule(s) could fire within "
                        f"{window} min -- suspend them in the Cloud UI before --execute")
    if name_matched_running:
        blockers.append("a RunFlow job matching this flow name is in flight -- wait")
    if distant:
        soonest = min(s["minutes_to_next_run"] for s in distant)
        advisories.append(
            f"{len(distant)} active schedule(s) exist but the next run is ~{soonest} min "
            f"away (beyond the {window}-min window). Safe to proceed if the backfill + "
            f"acceptance run finish before then; for a seam backfill the watermark is "
            f"preserved, so a later INCREMENTAL scheduled run does not duplicate. Confirm "
            f"the schedule run-type is Incremental (a Full run would duplicate regardless).")
    ready = not blockers

    print(f"flow {args.flow_luid} name={flow_name!r}")
    print(f"  active schedules:    {len(active)} "
          f"(imminent<={window}min: {len(imminent)}, distant: {len(distant)})")
    print(f"  suspended schedules: {len(suspended)}")
    print(f"  running RunFlow jobs: {len(jobs)} "
          f"(name-matched: {sum(1 for j in jobs if j.get('name_match'))})")
    print(f"  ready_for_overwrite: {ready}")
    for b in blockers:
        print(f"  BLOCKER: {b}")
    for a in advisories:
        print(f"  ADVISORY: {a}")

    print("RESULT_JSON: " + json.dumps({
        "status": "ok",
        "flow_luid": args.flow_luid,
        "flow_name": flow_name,
        "window_minutes": window,
        "ready_for_overwrite": ready,
        "blockers": blockers,
        "advisories": advisories,
        "active_schedules": active,
        "suspended_schedules": suspended,
        "running_flow_jobs": jobs,
    }, ensure_ascii=False))
    return 0 if ready else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
