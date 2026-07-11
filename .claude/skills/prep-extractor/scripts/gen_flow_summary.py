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

from inspect_actions import summarise_action  # noqa: E402

# nodeTypes this repo's toolchain understands (see references/tfl-json-schema.md).
# Anything else is processed anyway but flagged in Warnings.
KNOWN_NODE_TYPES = {
    "LoadSqlProxy", "LoadSql", "LoadHyper", "LoadCsv", "LoadExcel",
    "SuperTransform", "SuperJoin", "SuperUnion", "SuperAggregate",
    "SuperNewRows", "SuperPivot",
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


def actions_cell(node: dict) -> str:
    """Type-wise count summary for the Topology table, e.g. 'Rename×4, AddColumn×1'."""
    if not strip_type(node.get("nodeType", "")).endswith("SuperTransform"):
        return "—"
    counts = Counter(
        ACTION_LABEL.get(strip_type(an.get("nodeType", "")), strip_type(an.get("nodeType", "")))
        for an in action_annotations(node)
    )
    if not counts:
        return "0 actions"
    return ", ".join(f"{t}×{c}" for t, c in counts.items())


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
    total_actions = sum(len(action_annotations(nodes[n])) for n in order)
    type_counts = Counter(strip_type(nodes[n].get("nodeType", "")) for n in order)

    L: list[str] = []
    L.append(f"# Flow summary: {flow_name}")
    L.append("")

    # --- Meta ---
    L.append("## Meta")
    L.append(f"- Source: `{flow_path}`")
    L.append(f"- Flow name: {flow_name}")
    L.append(f"- Total nodes: {len(order)}")
    L.append(f"- Total actions (across SuperTransforms): {total_actions}")
    L.append("- Distinct nodeTypes: " + ", ".join(f"{t}({c})" for t, c in type_counts.items()))
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
    L.append("## SuperTransform actions inventory")
    L.append("")
    for nid in order:
        n = nodes[nid]
        if not strip_type(n.get("nodeType", "")).endswith("SuperTransform"):
            continue
        ans = action_annotations(n)
        L.append(f"### #{sid[nid]}: {n.get('name', '?')} ({len(ans)} actions)")
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
        for i, an in enumerate(action_annotations(n), 1):
            at = strip_type(an.get("nodeType", ""))
            if at not in KNOWN_ACTION_TYPES:
                warnings.append(
                    f"- ⚠️ Unknown action type: `{at}` at node #{sid[nid]} action {i}（raw JSON で残す）"
                )
        if ntype == "SuperTransform" and not action_annotations(n):
            warnings.append(
                f"- 💡 Empty SuperTransform: #{sid[nid]} ({n.get('name', '?')}) has 0 actions — 削除候補（decompose で判断）"
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
