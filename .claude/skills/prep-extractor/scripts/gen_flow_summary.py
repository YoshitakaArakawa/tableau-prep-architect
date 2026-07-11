#!/usr/bin/env python3
"""Generate a complete flow-summary.md (all 5 sections) from a flow.json.

Implements references/flow-summary-format.md mechanically: Meta / Topology /
Dependency DAG (Mermaid) / SuperTransform actions inventory / Warnings.
Phase A runs this instead of hand-assembling sections, so the summary is
always complete regardless of flow size.

Usage:
    python gen_flow_summary.py path/to/flow.json -o reports/flow-summary.md
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from inspect_actions import summarise_action  # noqa: E402
from flow_io import container_convertibility, iter_container_children  # noqa: E402

# nodeTypes this repo's toolchain understands (see references/tfl-json-schema.md).
# Anything else is processed anyway but flagged in Warnings.
# "Container" is the old-serialization Clean step (loomContainer with
# single-action children); its internals are surfaced like SuperTransform
# actions.
KNOWN_NODE_TYPES = {
    "LoadSqlProxy", "LoadSql", "LoadHyper", "LoadCsv", "LoadExcel",
    "SuperTransform", "SuperJoin", "SuperUnion", "SuperAggregate",
    "SuperNewRows", "SuperPivot", "Container",
    "PublishExtract", "WriteToHyper", "WriteToCsv",
}

# action nodeTypes summarise_action renders with a dedicated format; the
# generic raw-JSON fallback still renders others, but they get a Warning.
KNOWN_ACTION_TYPES = {
    "RenameColumn", "ChangeColumnType", "AddColumn", "RemoveColumns",
    "ValueFilter", "FilterOperation",
}

# Topology-table shorthand for action type counts.
ACTION_LABEL = {"RenameColumn": "Rename"}


def strip_type(node_type: str) -> str:
    return node_type.split(".")[-1] if node_type else "?"


def bfs_order(flow: dict) -> list[str]:
    """Short-ID ordering: BFS from initialNodes, then any unreached nodes."""
    nodes = flow["nodes"]
    visited: list[str] = []
    queue = list(flow.get("initialNodes", []))
    while queue:
        cur = queue.pop(0)
        if cur in visited or cur not in nodes:
            continue
        visited.append(cur)
        for nxt in nodes[cur].get("nextNodes", []) or []:
            nid = nxt.get("nextNodeId") if isinstance(nxt, dict) else nxt
            if nid and nid not in visited and nid not in queue:
                queue.append(nid)
    for nid in nodes:
        if nid not in visited:
            visited.append(nid)
    return visited


def next_ids(node: dict) -> list[str]:
    out = []
    for nxt in node.get("nextNodes", []) or []:
        nid = nxt.get("nextNodeId") if isinstance(nxt, dict) else nxt
        if nid:
            out.append(nid)
    return out


def action_annotations(node: dict) -> list[dict]:
    return [a.get("annotationNode", {}) for a in node.get("beforeActionAnnotations", []) or []]


def step_actions(node: dict) -> tuple[str, list[dict]]:
    """Unified per-node action list, annotationNode-shaped regardless of format.

    Returns (kind, actions) where kind is:
      - "flat"      : SuperTransform beforeActionAnnotations (new serialization)
      - "container" : .v1.Container loomContainer children (old serialization)
      - "input"     : Input node's `actions` (typically RenameColumn realizing
                      obfuscated field names into display captions)
      - ""          : node carries no step-level actions
    All three shapes hold the action fields (columnName / rename / expression /
    columnNames / filterExpression ...) directly on each dict, so
    summarise_action works on any of them.
    """
    ntype = strip_type(node.get("nodeType", ""))
    if ntype == "SuperTransform":
        return "flat", action_annotations(node)
    if ntype == "Container":
        return "container", iter_container_children(node)
    if node.get("baseType") == "input" and node.get("actions"):
        return "input", list(node["actions"])
    return "", []


def actions_cell(node: dict) -> str:
    """Type-wise count summary for the Topology table, e.g. 'Rename×4, AddColumn×1'."""
    kind, actions = step_actions(node)
    if not kind:
        return "—"
    if not actions:
        return "0 actions"
    counts = Counter(
        ACTION_LABEL.get(strip_type(an.get("nodeType", "")), strip_type(an.get("nodeType", "")))
        for an in actions
    )
    cell = ", ".join(f"{t}×{c}" for t, c in counts.items())
    return f"{cell} (input)" if kind == "input" else cell


def refresh_semantics(flow: dict) -> dict:
    """Extract incremental-refresh / append-output settings from nodeProperties.

    These live OUTSIDE the nodes dict (flow['nodeProperties'][<node-id>]) so a
    node-walk alone never sees them - yet they change what "parity" means:
    an append-mode output accumulates rows across runs, so the original PDS's
    total row count can never be reproduced by a single full run of the
    decomposed flows.

    Returns {"incremental_inputs": [(node_name, control_caption)],
             "append_outputs": [node_name]}.
    """
    nodes = flow.get("nodes") or {}

    def caption_of(field_uuid: str) -> str:
        for n in nodes.values():
            for f in n.get("fields") or []:
                if f.get("name") == field_uuid and f.get("caption"):
                    return f["caption"]
        return field_uuid

    inc_inputs: list[tuple[str, str]] = []
    append_outputs: list[str] = []
    for nid, props in (flow.get("nodeProperties") or {}).items():
        if not isinstance(props, dict):
            continue
        node_name = nodes.get(nid, {}).get("name", nid[:8])
        for pkey, pval in props.items():
            if not isinstance(pval, dict):
                continue
            short = pkey.split(".")[-1]
            if short == "IncrementalConfiguration" and pval.get("incrementalEnabled"):
                ctrl = pval.get("controlFieldName") or "?"
                inc_inputs.append((node_name, caption_of(ctrl)))
            if short == "OutputRefreshOptions":
                op = pval.get("outputOperationType") or ""
                if "Append" in op:
                    append_outputs.append(node_name)
    return {"incremental_inputs": inc_inputs, "append_outputs": append_outputs}


def mermaid_label(sid: int, ntype: str, name: str) -> str:
    # Quoted labels tolerate parens/brackets in Prep node names.
    safe = (name or "?").replace('"', "'")
    return f'  n{sid}["#{sid} {ntype} {safe}"]'


def build_summary(flow_path: Path, flow_name_override: str | None = None) -> str:
    flow = json.loads(flow_path.read_text(encoding="utf-8"))
    nodes = flow["nodes"]
    if not nodes:
        raise SystemExit("ERROR: flow['nodes'] is empty - not a Prep flow.json?")

    order = bfs_order(flow)
    sid = {nid: i + 1 for i, nid in enumerate(order)}
    prev_map: dict[str, list[str]] = {nid: [] for nid in nodes}
    for nid in order:
        for nxt in next_ids(nodes[nid]):
            if nxt in prev_map:
                prev_map[nxt].append(nid)

    # flow.json's top-level "name" is often the literal string "flow"; prefer
    # an explicit override (typically the .tfl filename stem).
    flow_name = flow_name_override or flow.get("name") or flow_path.stem
    if flow_name == "flow" and flow_name_override is None:
        flow_name = flow_path.stem
    kind_totals: Counter = Counter()
    for n in order:
        kind, actions = step_actions(nodes[n])
        if kind:
            kind_totals[kind] += len(actions)
    total_actions = sum(kind_totals.values())
    type_counts = Counter(strip_type(nodes[n].get("nodeType", "")) for n in order)

    L: list[str] = []
    L.append(f"# Flow summary: {flow_name}")
    L.append("")

    # --- Meta ---
    L.append("## Meta")
    L.append(f"- Source: `{flow_path}`")
    L.append(f"- Flow name: {flow_name}")
    L.append(f"- Total nodes: {len(order)}")
    breakdown = ", ".join(
        f"{label}: {kind_totals[k]}"
        for k, label in (("flat", "SuperTransform"), ("container", "Container"), ("input", "Input renames"))
        if kind_totals.get(k)
    ) or "none"
    L.append(f"- Total actions (across SuperTransforms): {total_actions} ({breakdown})")
    L.append("- Distinct nodeTypes: " + ", ".join(f"{t}({c})" for t, c in type_counts.items()))
    refresh = refresh_semantics(flow)
    if refresh["incremental_inputs"]:
        L.append("- Incremental inputs: " + ", ".join(
            f"{nm} (control field: {ctrl})" for nm, ctrl in refresh["incremental_inputs"]))
    if refresh["append_outputs"]:
        L.append("- Append-mode outputs: " + ", ".join(refresh["append_outputs"]))
    L.append(f"- Generated at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    L.append("")

    # --- Topology ---
    L.append("## Topology")
    L.append("")
    L.append("| # | UUID (short) | nodeType | Name | Prev | Next | Actions |")
    L.append("|---|---|---|---|---|---|---|")
    for nid in order:
        n = nodes[nid]
        prev_s = ", ".join(str(sid[p]) for p in prev_map[nid]) or "—"
        next_s = ", ".join(str(sid[x]) for x in next_ids(n) if x in sid) or "—"
        L.append(
            f"| {sid[nid]} | {nid[:6]}... | {strip_type(n.get('nodeType', ''))} "
            f"| {n.get('name', '?')} | {prev_s} | {next_s} | {actions_cell(n)} |"
        )
    L.append("")

    # --- Dependency DAG ---
    L.append("## Dependency DAG (Mermaid)")
    L.append("")
    L.append("```mermaid")
    L.append("graph TD")
    for nid in order:
        n = nodes[nid]
        L.append(mermaid_label(sid[nid], strip_type(n.get("nodeType", "")), n.get("name", "?")))
    L.append("")
    for nid in order:
        for nxt in next_ids(nodes[nid]):
            if nxt in sid:
                L.append(f"  n{sid[nid]} --> n{sid[nxt]}")
    L.append("```")
    L.append("")

    # --- Actions inventory ---
    # Covers all three action-carrying formats (see step_actions): flat
    # SuperTransform, old-serialization Container clean steps, and Input-node
    # rename actions. Section title kept stable for downstream consumers.
    KIND_TAG = {"container": " [container 形式]", "input": " [Input renames]"}
    L.append("## SuperTransform actions inventory")
    L.append("")
    for nid in order:
        n = nodes[nid]
        kind, ans = step_actions(n)
        if not kind:
            continue
        if kind == "input" and not ans:
            continue  # inputs without actions are not clean steps; skip silently
        L.append(
            f"### #{sid[nid]}: {n.get('name', '?')} ({len(ans)} actions){KIND_TAG.get(kind, '')}"
        )
        L.append("")
        if not ans:
            L.append("_(no actions — empty Clean step)_")
            L.append("")
            continue
        for i, an in enumerate(ans, 1):
            L.append(summarise_action(i, an))
        L.append("")

    # --- Warnings ---
    warnings: list[str] = []
    reachable = set()
    queue = list(flow.get("initialNodes", []))
    while queue:
        cur = queue.pop(0)
        if cur in reachable or cur not in nodes:
            continue
        reachable.add(cur)
        queue.extend(next_ids(nodes[cur]))

    name_counts = Counter(nodes[n].get("name", "?") for n in order)
    for nid in order:
        n = nodes[nid]
        ntype = strip_type(n.get("nodeType", ""))
        if ntype not in KNOWN_NODE_TYPES:
            warnings.append(
                f"- ⚠️ Unknown nodeType: `{ntype}` at node #{sid[nid]}（レイヤ推定保留、build 時は転写のみ）"
            )
        kind, actions = step_actions(n)
        for i, an in enumerate(actions, 1):
            at = strip_type(an.get("nodeType", ""))
            if at not in KNOWN_ACTION_TYPES:
                warnings.append(
                    f"- ⚠️ Unknown action type: `{at}` at node #{sid[nid]} action {i}（raw JSON で残す）"
                )
        if kind in ("flat", "container") and not actions:
            warnings.append(
                f"- 💡 Empty {'SuperTransform' if kind == 'flat' else 'Container'}: "
                f"#{sid[nid]} ({n.get('name', '?')}) has 0 actions — 削除候補（decompose で判断）"
            )
        if ntype == "Container":
            problems = container_convertibility(n)
            if problems:
                warnings.append(
                    f"- ⚠️ Container not convertible: #{sid[nid]} ({n.get('name', '?')}) — "
                    + "; ".join(problems)
                    + "（build 時は verbatim 転写のみ、actions 分割不可）"
                )
        if nid not in reachable:
            warnings.append(
                f"- 💡 Disconnected node: #{sid[nid]} ({n.get('name', '?')}) is not reachable from initialNodes"
            )
        if ntype == "SuperUnion":
            warnings.append(
                f"- 🔒 Node #{sid[nid]} {n.get('name', '?')} (SuperUnion): injects implicit "
                "`Table Names` column — do NOT propose deletion"
            )
    for name, c in name_counts.items():
        if c > 1:
            ids = ", ".join(f"#{sid[n]}" for n in order if nodes[n].get("name") == name)
            warnings.append(f"- 💡 Duplicate name: `{name}` appears at {ids}（build 時にファイル名を区別）")
    if refresh["incremental_inputs"] or refresh["append_outputs"]:
        inc_s = ", ".join(f"`{nm}` (control=`{ctrl}`)" for nm, ctrl in refresh["incremental_inputs"]) or "なし"
        app_s = ", ".join(f"`{nm}`" for nm in refresh["append_outputs"]) or "なし"
        warnings.append(
            f"- 🔒 Incremental/append flow: incremental input(s): {inc_s} / append output(s): {app_s}。"
            "append 出力の PDS は過去 run の累積のため **全体行数 parity は成立しない** — "
            "decompose で継承方針を設計し (self-check 項目 16)、compare は control field による期間一致で行う"
        )

    L.append("## Warnings")
    L.append("")
    L.extend(warnings if warnings else ["_(none)_"])
    L.append("")
    return "\n".join(L)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    p.add_argument("flow_path", type=Path, help="Path to flow.json (extracted from .tfl/.tflx)")
    p.add_argument("-o", "--output", type=Path, required=True, help="flow-summary.md output path")
    p.add_argument("--flow-name", help="Display name for the flow (e.g. the .tfl filename stem)")
    args = p.parse_args()

    text = build_summary(args.flow_path, flow_name_override=args.flow_name)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    print(f"Wrote {len(text):,} chars to: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
