#!/usr/bin/env python3
"""Read-only probe of flow scheduling state on Tableau Server/Cloud.

Answers, before designing (Phase A) and before verifying (Phase C):

  1. Which runFlow tasks exist, and are any already attached to our target
     flows (schedule collisions)?
  2. What Linked Tasks exist (member order via stepNumber, schedule state,
     frequency, trigger time)? Uses `GET /sites/{site}/tasks/linked` — note
     the resource name is `linked`, not `linkedTasks`.
  3. Are there stale look-alike flows on the server that could pollute the
     Linked Task flow picker in the UI (same name with a different LUID, or
     same layer prefix + same trailing token as a target flow)?

Limitations (Cloud REST, observed):
  - Weekday selection (frequencyDetails) is NOT exposed on flow tasks; only
    `frequency` + `nextRunAt`. The nextRunAt weekday is reported as partial
    evidence.
  - Per-step run-type (full/incremental) is NOT exposed anywhere in
    tasks/runFlow or tasks/linked. Run-type intent must come from the .tfl
    (collect_schedule_inputs.py); the applied setting is only observable
    behaviorally (row duplication after the first scheduled run).

Usage:
    python probe_flow_schedules.py --inputs <schedule-inputs.json> [--out probe.json]

Cloud access is read-only. Final line: RESULT_JSON: {...}
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))

import tableauserverclient as TSC  # noqa: E402

from tableau_auth import USER_AGENT, signed_in_server  # noqa: E402


def _get_json(server: Any, path: str) -> dict[str, Any]:
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


def _schedule_view(sch: dict[str, Any]) -> dict[str, Any]:
    return {
        "schedule_id": sch.get("id"),
        "schedule_name": sch.get("name"),
        "state": sch.get("state"),
        "type": sch.get("type"),
        "frequency": sch.get("frequency"),
        "next_run_at": sch.get("nextRunAt"),
    }


def list_run_flow_tasks(server: Any) -> list[dict[str, Any]]:
    data = _get_json(server, "tasks/runFlow")
    if "_http_error" in data:
        raise RuntimeError(f"GET tasks/runFlow failed: {data}")
    tasks = []
    for t in (data.get("tasks") or {}).get("task", []):
        fr = t.get("flowRun") or {}
        tasks.append({
            "task_id": fr.get("id"),
            "flow_luid": (fr.get("flow") or {}).get("id"),
            "flow_name": (fr.get("flow") or {}).get("name"),
            **_schedule_view(fr.get("schedule") or {}),
        })
    return tasks


def list_linked_tasks(server: Any) -> list[dict[str, Any]]:
    data = _get_json(server, "tasks/linked")
    if "_http_error" in data:
        raise RuntimeError(f"GET tasks/linked failed: {data}")
    linked = []
    outer = (data.get("linkedTasks") or {}).get("linkedTasks", [])
    for lt in outer:
        steps = []
        for st in ((lt.get("linkedTaskSteps") or {}).get("linkedTaskSteps") or []):
            fr = ((st.get("task") or {}).get("flowRun")) or {}
            steps.append({
                "step_number": int(st.get("stepNumber", 0)),
                "flow_luid": (fr.get("flow") or {}).get("id"),
                "flow_name": (fr.get("flow") or {}).get("name"),
                "task_id": fr.get("id"),
                "stop_downstream_on_failure": st.get("stopDownstreamTasksOnFailure"),
            })
        steps.sort(key=lambda s: s["step_number"])
        linked.append({
            "linked_task_id": lt.get("id"),
            "num_steps": int(lt.get("numSteps", len(steps))),
            **_schedule_view(lt.get("schedule") or {}),
            "steps": steps,
        })
    return linked


def find_stale_lookalikes(server: Any, targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Server flows that could be mistaken for a target flow in the UI picker.

    Flags: (a) same name, different LUID; (b) same layer prefix (stg/int/fct)
    AND same final underscore-token but not a target itself (advisory — catches
    stale pipeline generations like <prefix>_old_entity_<same-suffix>).
    """
    target_by_luid = {t["flow_luid"]: t for t in targets if t.get("flow_luid")}
    target_names = {t["name"] for t in targets}

    def key(name: str) -> tuple[str, str] | None:
        parts = name.split("_")
        if len(parts) < 2 or parts[0] not in ("stg", "int", "fct"):
            return None
        return (parts[0], parts[-1])

    target_keys = {k for k in (key(n) for n in target_names) if k}
    stale = []
    for flow in TSC.Pager(server.flows):
        if flow.id in target_by_luid:
            continue
        reason = None
        if flow.name in target_names:
            reason = "same name as a target flow but different LUID"
        else:
            k = key(flow.name or "")
            if k and k in target_keys:
                reason = f"layer prefix + trailing token collide with a target ({k[0]}_*_{k[1]})"
        if reason:
            stale.append({
                "flow_luid": flow.id,
                "flow_name": flow.name,
                "project": flow.project_name,
                "webpage_url": flow.webpage_url,
                "reason": reason,
            })
    return stale


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--inputs", required=True, help="schedule-inputs.json from collect_schedule_inputs.py")
    ap.add_argument("--out", help="write probe JSON here (default: stdout)")
    ap.add_argument("--skip-stale-scan", action="store_true",
                    help="skip the all-flows look-alike scan (faster on large sites)")
    args = ap.parse_args()

    inputs = json.loads(Path(args.inputs).read_text(encoding="utf-8"))
    targets = inputs["flows"]
    target_luids = {t["flow_luid"] for t in targets if t.get("flow_luid")}

    with signed_in_server() as server:
        run_tasks = list_run_flow_tasks(server)
        linked = list_linked_tasks(server)
        stale = [] if args.skip_stale_scan else find_stale_lookalikes(server, targets)

    on_targets = [t for t in run_tasks if t["flow_luid"] in target_luids]
    result = {
        "schema_version": "1",
        "tasks_on_target_flows": on_targets,
        "linked_tasks": linked,
        "all_run_flow_tasks": run_tasks,
        "stale_lookalike_flows": stale,
    }
    payload = json.dumps(result, indent=2, ensure_ascii=False)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload, encoding="utf-8")
        print(f"probe written -> {out}")
    else:
        print(payload)

    print("RESULT_JSON: " + json.dumps({
        "status": "ok",
        "tasks_on_target_flows": len(on_targets),
        "linked_tasks": len(linked),
        "run_flow_tasks": len(run_tasks),
        "stale_lookalikes": len(stale),
        "out": args.out,
    }))


if __name__ == "__main__":
    main()
