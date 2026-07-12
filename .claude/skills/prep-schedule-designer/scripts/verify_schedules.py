#!/usr/bin/env python3
"""Verify UI-created Linked Tasks against the schedule design (Phase C).

Compares `schedule-design.json` (written in Phase B, schema in
references/runbook-format.md) with the live server state read via
probe_flow_schedules.py's endpoints, and writes a Markdown report.

Machine-checkable per domain:
  - a Linked Task exists whose member flow LUIDs match the design steps
  - member order (stepNumber) matches the designed order
  - schedule state is Active, frequency matches, trigger time-of-day (UTC)
    of nextRunAt matches
  - weekday: partial only — REST hides frequencyDetails, so the check is
    "nextRunAt's weekday is within the designed weekdays"
Cross-domain:
  - every designed flow is scheduled exactly once; extra tasks on target
    flows are flagged
  - old schedules slated for removal are listed with their current state

NOT machine-checkable (REST hides per-step run-type): whether Incremental
refresh was selected on each incremental step. The report emits a behavioral
checklist instead: after the first scheduled run, an incremental flow's
append output must not double (period-count via the control field).

Usage:
    python verify_schedules.py --design <schedule-design.json> \
        --out <schedule-verify-report.md>

Cloud access is read-only. Exit 0 with verdict in RESULT_JSON (pass/fail).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))

from tableau_auth import signed_in_server  # noqa: E402

# Reuse the probe's REST readers (same skill directory).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_flow_schedules import list_linked_tasks, list_run_flow_tasks  # noqa: E402

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _parse_next_run(next_run_at: str | None) -> datetime | None:
    if not next_run_at:
        return None
    try:
        return datetime.fromisoformat(next_run_at.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def match_linked_task(domain: dict[str, Any], linked: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the linked task with the largest member-LUID overlap (ties: first)."""
    want = {s["flow_luid"] for s in domain["steps"]}
    best, best_overlap = None, 0
    for lt in linked:
        got = {s["flow_luid"] for s in lt["steps"]}
        overlap = len(want & got)
        if overlap > best_overlap:
            best, best_overlap = lt, overlap
    return best


def verify_domain(domain: dict[str, Any], linked: list[dict[str, Any]]) -> dict[str, Any]:
    issues: list[str] = []
    lt = match_linked_task(domain, linked)
    if lt is None:
        return {"domain": domain["name"], "linked_task_id": None,
                "issues": ["no Linked Task found containing any designed member"],
                "verdict": "fail"}

    want_order = [s["flow_luid"] for s in sorted(domain["steps"], key=lambda s: s["order"])]
    got_order = [s["flow_luid"] for s in lt["steps"]]

    missing = [l for l in want_order if l not in got_order]
    extra = [l for l in got_order if l not in want_order]
    if missing:
        names = [s["flow_name"] for s in domain["steps"] if s["flow_luid"] in missing]
        issues.append(f"members missing from Linked Task: {names}")
    if extra:
        names = [s["flow_name"] for s in lt["steps"] if s["flow_luid"] in extra]
        issues.append(f"unexpected members in Linked Task: {names}")

    common_want = [l for l in want_order if l in got_order]
    common_got = [l for l in got_order if l in want_order]
    if common_want != common_got:
        issues.append("member order differs from design (dependency order may be violated)")

    trig = domain.get("trigger") or {}
    if trig.get("frequency") and lt.get("frequency") != trig["frequency"]:
        issues.append(f"frequency: designed {trig['frequency']}, server {lt.get('frequency')}")
    if lt.get("state") != "Active":
        issues.append(f"schedule state is {lt.get('state')} (expected Active)")

    nra = _parse_next_run(lt.get("next_run_at"))
    if trig.get("time_utc") and nra:
        got_hm = nra.strftime("%H:%M")
        if got_hm != trig["time_utc"]:
            issues.append(f"trigger time (UTC): designed {trig['time_utc']}, nextRunAt says {got_hm}")
    wd = trig.get("weekdays")
    if wd and wd != "every_day" and nra:
        day = WEEKDAYS[nra.weekday()]
        if day not in wd:
            issues.append(f"nextRunAt falls on {day}, outside designed weekdays {wd} "
                          "(partial check: REST hides the weekday selection)")

    return {"domain": domain["name"], "linked_task_id": lt["linked_task_id"],
            "schedule_state": lt.get("state"), "next_run_at": lt.get("next_run_at"),
            "issues": issues, "verdict": "pass" if not issues else "fail"}


def render_report(results: list[dict[str, Any]],
                  cross: list[str], old_rows: list[dict[str, Any]],
                  incr_steps: list[dict[str, Any]]) -> str:
    lines = ["# Schedule verify report", ""]
    overall = "pass" if not cross and all(r["verdict"] == "pass" for r in results) else "fail"
    lines += [f"**overall_verdict: {overall}**", ""]

    lines += ["## Domains", ""]
    for r in results:
        lines.append(f"### {r['domain']} — {r['verdict']}")
        lines.append(f"- linked_task_id: `{r.get('linked_task_id')}` / state: {r.get('schedule_state')} "
                     f"/ nextRunAt: {r.get('next_run_at')}")
        for i in r["issues"]:
            lines.append(f"- ⚠️ {i}")
        lines.append("")

    if cross:
        lines += ["## Cross-domain issues", ""]
        lines += [f"- ⚠️ {c}" for c in cross]
        lines.append("")

    if old_rows:
        lines += ["## Old schedules slated for removal (current state)", ""]
        lines += ["| id | name | state |", "|---|---|---|"]
        lines += [f"| `{o['id']}` | {o.get('name','')} | {o.get('state','(not found)')} |" for o in old_rows]
        lines.append("")

    lines += [
        "## Run-type behavioral checklist (REST cannot read per-step run-type)",
        "",
        "For each incremental step below, after the FIRST scheduled run completes:",
        "verify the append output did NOT duplicate — count rows within the",
        "control-field period (e.g. via prep-output-comparator's period count or",
        "query-datasource) and compare against the pre-run count for the same period.",
        "A doubled count means the step ran as Full refresh: fix the run-type in the",
        "Linked Task UI, then repair the output (delete PDS → baseline full run →",
        "incremental thereafter).",
        "",
        "| domain | flow | control field |",
        "|---|---|---|",
    ]
    lines += [f"| {s['domain']} | {s['flow_name']} | {s.get('control_field') or '-'} |" for s in incr_steps]
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--design", required=True, help="schedule-design.json (Phase B output)")
    ap.add_argument("--out", required=True, help="markdown report output path")
    args = ap.parse_args()

    design = json.loads(Path(args.design).read_text(encoding="utf-8"))
    domains = design.get("domains") or []
    if not domains:
        print("ERROR: design has no domains", file=sys.stderr)
        print("RESULT_JSON: " + json.dumps({"status": "error", "message": "no domains in design"}))
        sys.exit(1)

    with signed_in_server() as server:
        linked = list_linked_tasks(server)
        run_tasks = list_run_flow_tasks(server)

    results = [verify_domain(d, linked) for d in domains]

    # cross-domain: each designed flow scheduled exactly once
    cross: list[str] = []
    designed_luids: dict[str, str] = {}
    for d in domains:
        for s in d["steps"]:
            designed_luids[s["flow_luid"]] = s["flow_name"]
    count_by_luid: dict[str, int] = {}
    linked_member_task_ids: set[str] = set()
    for lt in linked:
        for s in lt["steps"]:
            count_by_luid[s["flow_luid"]] = count_by_luid.get(s["flow_luid"], 0) + 1
            if s.get("task_id"):
                linked_member_task_ids.add(s["task_id"])
    for luid, name in designed_luids.items():
        n = count_by_luid.get(luid, 0)
        if n == 0:
            cross.append(f"{name}: not a member of any Linked Task")
        elif n > 1:
            cross.append(f"{name}: member of {n} Linked Tasks (double-fire)")
    # a runFlow task on a designed flow that is NOT a linked-task member is a
    # standalone schedule firing in parallel with the chain (double-fire).
    for t in run_tasks:
        if t["flow_luid"] in designed_luids and t["task_id"] not in linked_member_task_ids:
            cross.append(
                f"{designed_luids[t['flow_luid']]}: standalone runFlow task "
                f"{t['task_id']} (schedule {t.get('schedule_name')}, state {t.get('state')}) "
                "fires outside the Linked Task"
            )

    old_specs = design.get("old_schedules_to_remove") or []
    state_by_sched = {}
    for lt in linked:
        state_by_sched[lt["schedule_id"]] = lt.get("state")
    for t in run_tasks:
        state_by_sched.setdefault(t["schedule_id"], t.get("state"))
    old_rows = [{**o, "state": state_by_sched.get(o["id"])} for o in old_specs]

    incr_steps = [
        {"domain": d["name"], "flow_name": s["flow_name"], "control_field": s.get("control_field")}
        for d in domains for s in d["steps"]
        if s.get("run_type") == "incremental"
    ]

    report = render_report(results, cross, old_rows, incr_steps)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")

    overall = "pass" if not cross and all(r["verdict"] == "pass" for r in results) else "fail"
    print(f"verify report -> {out} (overall: {overall})")
    print("RESULT_JSON: " + json.dumps({
        "status": "ok",
        "overall_verdict": overall,
        "domains": {r["domain"]: r["verdict"] for r in results},
        "cross_domain_issues": len(cross),
        "out": str(out),
    }))


if __name__ == "__main__":
    main()
