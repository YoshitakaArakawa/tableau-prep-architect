#!/usr/bin/env python3
"""Render migration-plan.json into the human-facing migration-plan.md.

The JSON is the source of truth; this md is regenerated, never hand-edited
(same model as decomposition-plan). Nullable sections that are still null are
shown as `(＜stage＞で確定)` placeholders so unfilled work stays visible — that
visibility is the whole point of the忘れ防止 ledger. single/multi branches on
meta.flow_count. See references/plan-format.md for the template spec.

Usage:
    python render_migration_plan.py --plan reports/migration-plan.json \
        -o reports/migration-plan.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# matrix 列: パイプライン 6 工程 + 横断 3 工程。(表示ラベル, json キー) の対で持つ
PIPELINE_COLS = [("extr", "extract"), ("anlz", "analyze"), ("dcmp", "decompose"),
                 ("bild", "build"), ("pub", "publish"), ("cmp", "compare")]
CROSSCUT_COLS = [("sched", "schedule"), ("repnt", "repoint"), ("bkfl", "backfill")]
# status → 短縮表示。pipeline の n/a は対象外を表す "―"、crosscut は "n/a" のまま
STATUS_ABBR = {"pending": "○", "in_progress": "wip", "done": "done",
               "fail": "fail", "partial": "part", "n/a": "n/a"}


def abbr(status: str, is_pipeline: bool) -> str:
    if status == "n/a" and is_pipeline:
        return "―"
    return STATUS_ABBR.get(status, status)


def render_matrix(matrix: dict) -> list[str]:
    rows = matrix.get("rows") or []
    if not rows:
        return ["(decompose 後に分解後 .tfl 単位で描画。status はそこから追跡)"]
    name_w = max(16, max(len(r.get("tfl", "")) for r in rows))
    cell_w = 5
    pipe_hdr = "".join(lbl.center(cell_w) for lbl, _ in PIPELINE_COLS)
    cross_hdr = "".join(lbl.center(cell_w) for lbl, _ in CROSSCUT_COLS)
    L = [f"{'':<{name_w}} │{pipe_hdr}│{cross_hdr}",
         f"{'─' * name_w}─┼{'─' * len(pipe_hdr)}┼{'─' * len(cross_hdr)}"]
    for r in rows:
        pipe = r.get("pipeline", {})
        cross = r.get("crosscut", {})
        pcells = "".join(abbr(pipe.get(k, "n/a"), True).center(cell_w) for _, k in PIPELINE_COLS)
        ccells = "".join(abbr(cross.get(k, "n/a"), False).center(cell_w) for _, k in CROSSCUT_COLS)
        L.append(f"{r.get('tfl', '?'):<{name_w}} │{pcells}│{ccells}")
    L.append("凡例: ○=適用/pending ―=対象外(stg) done/fail/part=進捗 "
             "wip=進行中 n/a=非該当")
    return L


def render_trigger_policy(tp) -> list[str]:
    if not tp:
        return ["(schedule 工程で確定 / crosscut に schedule が無ければ N/A)"]
    if isinstance(tp, str):  # prep-schedule-designer の trigger_policy は散文契約
        return [tp]
    L = []
    if tp.get("tz"):
        L.append(f"- tz: {tp['tz']}")
    for d in tp.get("domains", []) or []:
        cons = d.get("weekday_constraint") or "制約なし"
        L.append(f"- {d.get('name', '?')}: {cons}")
    return L or [json.dumps(tp, ensure_ascii=False)]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--plan", required=True, type=Path)
    p.add_argument("-o", "--out", required=True, type=Path)
    args = p.parse_args()

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    meta = plan.get("meta", {})
    is_single = meta.get("flow_count") == "single"

    L: list[str] = [f"# Migration Plan — {meta.get('target_path', '?')}   "
                    f"(created: {meta.get('created_marker', '?')})", ""]
    L += ["> Rendered from migration-plan.json by render_migration_plan.py — "
          "edit the JSON, not this file.", ""]

    # Scope
    scope = plan.get("scope", {})
    in_s = ", ".join(scope.get("in_scope", [])) or "—"
    out_s = ", ".join(scope.get("out_of_scope", [])) or "(なし)"
    L += ["## Scope",
          f"in-scope: {in_s}    out-of-scope: {out_s}    goal: {meta.get('goal_stage', '?')}", ""]

    # Migration order (multi only)
    if not is_single:
        mo = plan.get("migration_order", {})
        src = mo.get("source")
        L.append(f"## Migration order  ← 根拠: {src}" if src else "## Migration order")
        order = mo.get("order", [])
        L.append("  ".join(f"{i}. {n}" for i, n in enumerate(order, 1)) + "   (producer 先行)"
                 if order else "—")
        L.append("")
        batches = plan.get("session_batches")
        if batches:
            L += ["## Session batches"]
            for b in batches:
                L.append(f"- {b.get('tag', '?')}: {', '.join(b.get('flows', []))} "
                         f"(reuse: {b.get('reuse', '—')})")
            L.append("")

    # Matrix
    L += ["## Migration matrix"] + render_matrix(plan.get("matrix", {})) + [""]

    # Trigger policy
    L += ["## Trigger policy"] + render_trigger_policy(plan.get("trigger_policy")) + [""]

    # Backfill
    L += ["## Backfill"]
    cands = plan.get("backfill_candidates", [])
    if cands:
        for c in cands:
            cf = c.get("control_field") or "?"
            mode = c.get("mode") or "compare 後に決定"
            L.append(f"- {c.get('flow', '?')} (control={cf}): mode={mode}")
    else:
        L.append("候補: (なし)")
    L.append("")

    # Human queue
    L += ["## Human work queue"]
    queue = plan.get("human_queue", [])
    if queue:
        for q in queue:
            ref = q.get("runbook_ref") or "(runbook 待ち)"
            L.append(f"{q.get('step', '?')}. [{q.get('trigger_condition', '?')}]  "
                     f"{q.get('action', '?')}   → {ref}  <{q.get('status', 'pending')}>")
    else:
        L.append("(横断工程なし)")
    L.append("")

    # Pointers
    ptr = plan.get("pointers", {})
    fd = ptr.get("flow_dependencies") or "—"
    dc = ptr.get("deploy_context") or "—"
    nm = len(ptr.get("manifests", []))
    L += ["## Pointers（ファクトの出所 — 転記しない）",
          f"{fd} / {dc} / manifests[{nm} 件]", ""]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(L)
    args.out.write_text(text, encoding="utf-8")
    print(f"Wrote {len(text):,} chars to: {args.out}", file=sys.stderr)
    print(f"RESULT_JSON: {json.dumps({'out': str(args.out), 'single': is_single})}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
