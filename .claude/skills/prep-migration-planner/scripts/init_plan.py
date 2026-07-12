#!/usr/bin/env python3
"""Generate the skeleton migration-plan.json for step 0b.

Fills the init-mandatory sections (scope / migration_order / backfill_candidates
/ human_queue skeleton / pointers) from mechanical inputs + intake values, and
leaves the nullable sections (trigger_policy / old_schedule_notes / matrix.rows)
empty for later progressive fill. See references/plan-format.md for the schema
and references/orchestration-model.md for the decision-ledger rationale.

Backfill candidates are detected from incremental config (the flow_io canonical
logic, same as prep-schedule-designer): a flow whose run_type is "incremental"
is a candidate. Two input paths:

  - multi-flow: read map_flow_dependencies.py --json facts (incremental column
    included), no raw flows needed
  - single-flow: read the one raw flow and run get_incremental_config directly

Usage:
    # multi-flow
    python init_plan.py --flow-deps-json reports/flow-dependencies.json \
        --flow-deps-md reports/flow-dependencies.md \
        --deploy-context reports/deploy-context.md \
        --goal 5 --target "99_Sandbox/x_decompose" --flow-count multi \
        --scope-in "A,B,C" --crosscut "schedule,repoint" \
        --session-batch "20260712_batch1:A,B" --session-batch "20260713_batch2:C" \
        --out reports/migration-plan.json

    # single-flow
    python init_plan.py --flow work/session/flow.json \
        --goal 5 --target "99_Sandbox/x_decompose" --flow-count single \
        --scope-in "A" --crosscut "repoint" --out reports/migration-plan.json
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from flow_io import get_incremental_config  # noqa: E402

# Q2a の goal 深度 (prep-migrate Session intake) を表示ラベルに対応させる
GOAL_LABELS = {
    1: "① 分析のみ",
    2: "② 分解設計まで",
    3: "③ .tfl 生成まで",
    4: "④ publish & run まで",
    5: "⑤ E2E 比較まで",
}
# JST 固定 (個人ルール: 時刻は JST、日付は YYYYMMDD)
JST = timezone(timedelta(hours=9))
# human_queue の骨テンプレート (中身は各 runbook 生成時に runbook_ref で埋める)
QUEUE_TEMPLATES = {
    "schedule": ("各 int/mart publish 後", "Linked Task を UI 作成"),
    "repoint": ("該当フロー publish 後", "Replace Data Source で WB 差し替え"),
    "backfill": ("compare 後・承認時のみ", "backfill 本番 Overwrite"),
}


def load_flow(path: Path) -> dict:
    """Load flow JSON from a .tfl/.tflx (zip entry 'flow') or a bare .json."""
    if path.suffix.lower() in (".tfl", ".tflx"):
        with zipfile.ZipFile(path) as z:
            return json.loads(z.read("flow").decode("utf-8"))
    return json.loads(path.read_text(encoding="utf-8"))


def split_csv(value: str | None) -> list[str]:
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def candidate_from_incremental(flow_name: str, inc: dict) -> dict | None:
    """Build a backfill candidate entry from a get_incremental_config result
    (or the same shape carried in facts). Returns None if not incremental."""
    if not inc or inc.get("run_type") != "incremental":
        return None
    fields = inc.get("control_fields") or []
    cf = fields[0] if fields else None
    reason = f"incremental/append 検出 (control={cf})" if cf else "incremental/append 検出"
    return {"flow": flow_name, "control_field": cf,
            "applicable": True, "mode": None, "reason": reason}


def parse_session_batches(raw: list[str]) -> list[dict]:
    """Parse --session-batch 'tag:flowA,flowB' entries."""
    batches = []
    for entry in raw:
        if ":" not in entry:
            sys.exit(f"ERROR: --session-batch must be 'tag:flowA,flowB', got: {entry}")
        tag, flows_csv = entry.split(":", 1)
        batches.append({
            "tag": tag.strip(),
            "flows": split_csv(flows_csv),
            "reuse": "deploy-context.md",
        })
    return batches


def build_backfill_candidates(args, in_scope: list[str]) -> list[dict]:
    """multi: read incremental from facts json. single: read the raw flow."""
    candidates: list[dict] = []
    if args.flow_deps_json:
        deps = json.loads(args.flow_deps_json.read_text(encoding="utf-8"))
        by_name = {f.get("name"): f for f in deps.get("flows", [])}
        for name in in_scope:
            fact = by_name.get(name)
            if not fact:
                continue  # in-scope フローが facts に無い (名前ずれ) — 候補判定はスキップ
            cand = candidate_from_incremental(name, fact.get("incremental") or {})
            if cand:
                candidates.append(cand)
    elif args.flow:
        # 単発: flow 名は scope から (get_incremental_config は flow 単位)
        name = in_scope[0] if in_scope else args.flow.stem
        inc = get_incremental_config(load_flow(args.flow))
        cand = candidate_from_incremental(name, inc)
        if cand:
            candidates.append(cand)
    return candidates


def build_human_queue(crosscut: set[str], has_backfill_candidate: bool) -> list[dict]:
    """Skeleton queue from --crosscut plus auto backfill step if candidates exist."""
    order = ["schedule", "repoint", "backfill"]
    active = [k for k in order if k in crosscut]
    if has_backfill_candidate and "backfill" not in active:
        active.append("backfill")
    queue = []
    for i, key in enumerate(active, start=1):
        trigger, action = QUEUE_TEMPLATES[key]
        queue.append({"step": i, "trigger_condition": trigger,
                      "action": action, "status": "pending", "runbook_ref": None})
    return queue


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--flow-deps-json", type=Path,
                   help="multi: map_flow_dependencies.py --json output (facts with incremental)")
    p.add_argument("--flow", type=Path,
                   help="single: one raw flow (.tfl/.tflx/flow.json) for backfill detection")
    p.add_argument("--flow-deps-md", type=Path, help="pointer for migration_order.source")
    p.add_argument("--deploy-context", type=Path, help="pointer for pointers.deploy_context")
    p.add_argument("--goal", type=int, required=True, choices=range(1, 6),
                   help="Q2a goal stage 1-5")
    p.add_argument("--target", required=True, help="target project path")
    p.add_argument("--flow-count", required=True, choices=("single", "multi"))
    p.add_argument("--scope-in", required=True, help="in-scope flows, comma-separated")
    p.add_argument("--scope-out", help="out-of-scope flows, comma-separated")
    p.add_argument("--crosscut", help="cross-cut steps for human_queue: schedule,repoint,backfill")
    p.add_argument("--session-batch", action="append", default=[],
                   help="multi: 'tag:flowA,flowB' (repeatable)")
    p.add_argument("-o", "--out", type=Path, required=True)
    args = p.parse_args()

    if args.flow_count == "multi" and not args.flow_deps_json:
        p.error("--flow-count multi requires --flow-deps-json (run map_flow_dependencies.py --json first)")
    if args.flow_count == "single" and not args.flow:
        p.error("--flow-count single requires --flow (the raw flow for backfill detection)")

    in_scope = split_csv(args.scope_in)
    if not in_scope:
        p.error("--scope-in is empty")
    out_scope = split_csv(args.scope_out)
    crosscut = set(split_csv(args.crosscut))

    # migration_order: multi は facts の topological、single は自明
    if args.flow_deps_json:
        deps = json.loads(args.flow_deps_json.read_text(encoding="utf-8"))
        topo = deps.get("topological_order") or in_scope
        order = [f for f in topo if f in in_scope] or in_scope
        order_source = args.flow_deps_md.name if args.flow_deps_md else "flow-dependencies.md"
    else:
        order = in_scope
        order_source = None

    candidates = build_backfill_candidates(args, in_scope)
    human_queue = build_human_queue(crosscut, bool(candidates))
    batches = (parse_session_batches(args.session_batch)
               if args.flow_count == "multi" and args.session_batch else None)

    plan = {
        "meta": {
            "target_path": args.target,
            "goal_stage": GOAL_LABELS[args.goal],
            "flow_count": args.flow_count,
            "created_marker": datetime.now(JST).strftime("%Y%m%d %H:%M:%S JST"),
        },
        "scope": {"in_scope": in_scope, "out_of_scope": out_scope},
        "migration_order": {"order": order, "source": order_source},
        "session_batches": batches,
        "backfill_candidates": candidates,
        "trigger_policy": None,
        "old_schedule_notes": None,
        "matrix": {"rendered_after": "decompose", "rows": []},
        "human_queue": human_queue,
        "pointers": {
            "flow_dependencies": args.flow_deps_md.name if args.flow_deps_md else None,
            "deploy_context": args.deploy_context.name if args.deploy_context else None,
            "manifests": [],
        },
        "status_note": ("status fields are a re-derivable cache; "
                        "reconcile against manifests on resume"),
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {"out": str(args.out), "flow_count": args.flow_count,
              "in_scope": len(in_scope), "backfill_candidates": len(candidates),
              "human_queue_steps": len(human_queue)}
    print(f"Wrote migration-plan skeleton to: {args.out}", file=sys.stderr)
    print(f"RESULT_JSON: {json.dumps(result)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
