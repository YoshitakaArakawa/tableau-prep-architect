#!/usr/bin/env python3
"""Phase C helper: build a cross-flow dependency map for a set of Prep flows.

For each flow, extracts output PDS names (PublishExtract nodes), inputs
(classified via flow_io.inspect_input_node), and incremental/append config
(flow_io.get_incremental_config, for backfill-candidate detection), then derives:
  - in-scope dependency edges (flow A consumes flow B's output PDS)
  - a topological migration order (roots with no in-scope deps first)
  - shared vconn tables read by 2+ flows (stg reuse candidates)

Guessing migration order from flow names or domain intuition gets it wrong
(a "stats" flow may consume the "incremental" flow's output, not vice versa);
this extraction is mechanical and decisive. Run it once when planning a
multi-flow migration, and re-run only when the flow set changes - the map is
stable within a project, so per-session (Phase A/B) invocation is wasteful.

Edge matching is by output PDS *name* (datasourceName). If two in-scope flows
publish the same PDS name the edge is ambiguous and reported as a warning
instead of an edge.

Usage:
    # Local mode: point at flow files and/or directories containing them
    python map_flow_dependencies.py work/session1/flow.json work/tfl_dir \
        -o reports/flow-dependencies.md

    # Server mode: download every flow in a project first, then map
    python map_flow_dependencies.py --project "1_Prep" --download-dir scratch/flows \
        -o reports/flow-dependencies.md
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))

from flow_io import inspect_input_node, get_incremental_config  # noqa: E402

FLOW_SUFFIXES = {".tfl", ".tflx", ".json"}


def load_flow(path: Path) -> dict:
    """Load flow JSON from a .tfl/.tflx (zip entry 'flow') or a bare .json."""
    if path.suffix.lower() in (".tfl", ".tflx"):
        with zipfile.ZipFile(path) as z:
            return json.loads(z.read("flow").decode("utf-8"))
    return json.loads(path.read_text(encoding="utf-8"))


def collect_flow_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files.extend(sorted(
                f for f in p.iterdir()
                if f.is_file() and f.suffix.lower() in FLOW_SUFFIXES
            ))
        elif p.is_file():
            files.append(p)
        else:
            sys.exit(f"ERROR: path not found: {p}")
    if not files:
        sys.exit("ERROR: no flow files found in the given paths")
    return files


def extract_flow_facts(name: str, flow: dict) -> dict:
    """Mechanical per-flow extraction: outputs + classified inputs."""
    nodes = flow.get("nodes") or {}
    outputs = []
    for n in nodes.values():
        if n.get("nodeType", "").endswith("PublishExtract"):
            outputs.append({
                "datasource_name": n.get("datasourceName"),
                "project_name": n.get("projectName"),
            })
    inputs_pds = []
    inputs_vconn = []
    inputs_other = []
    for nid, n in nodes.items():
        if n.get("baseType") != "input":
            continue
        r = inspect_input_node(flow, nid)
        kind = r.get("kind")
        if kind == "pds":
            attrs = n.get("connectionAttributes") or {}
            inputs_pds.append({
                "datasource_name": attrs.get("datasourceName") or n.get("name"),
                "project_name": attrs.get("projectName"),
            })
        elif kind == "vconn":
            inputs_vconn.append({
                "vconn_luid": r.get("vconn_luid"),
                "vconn_caption": r.get("vconn_caption"),
                "table_uuid": r.get("table_uuid"),
                "table_name": r.get("table_name"),
            })
        else:  # direct_db / extract / unknown - listed for completeness
            inputs_other.append({"kind": kind, "node_name": n.get("name")})
    inc = get_incremental_config(flow)
    return {
        "name": name,
        "outputs": outputs,
        "inputs_pds": inputs_pds,
        "inputs_vconn": inputs_vconn,
        "inputs_other": inputs_other,
        "incremental": {"run_type": inc["run_type"], "control_fields": inc["control_fields"]},
    }


def build_edges(facts: list[dict]) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Match input PDS names to output PDS names across flows.

    Returns (edges, warnings) where each edge is
    (consumer_flow, producer_flow, pds_name).
    """
    warnings: list[str] = []
    producer_by_pds: dict[str, list[str]] = defaultdict(list)
    for f in facts:
        for out in f["outputs"]:
            if out["datasource_name"]:
                producer_by_pds[out["datasource_name"]].append(f["name"])
    for pds, producers in producer_by_pds.items():
        if len(producers) > 1:
            warnings.append(
                f"PDS name `{pds}` is published by multiple flows "
                f"({', '.join(producers)}) - edges via this PDS are skipped as ambiguous"
            )
    edges: list[tuple[str, str, str]] = []
    for f in facts:
        for inp in f["inputs_pds"]:
            pds = inp["datasource_name"]
            producers = producer_by_pds.get(pds, [])
            if len(producers) == 1 and producers[0] != f["name"]:
                edges.append((f["name"], producers[0], pds))
    return edges, warnings


def topological_order(facts: list[dict], edges: list[tuple[str, str, str]]) -> tuple[list[str], list[str]]:
    """Kahn's algorithm; producers come before consumers.

    Returns (ordered_names, cycle_members). Cycle members (if any) are
    appended after the ordered part and reported by the caller.
    """
    names = [f["name"] for f in facts]
    deps: dict[str, set[str]] = {n: set() for n in names}
    for consumer, producer, _ in edges:
        deps[consumer].add(producer)
    ordered: list[str] = []
    remaining = dict(deps)
    while remaining:
        roots = sorted(n for n, d in remaining.items() if not (d & set(remaining)))
        if not roots:
            return ordered, sorted(remaining)  # cycle
        ordered.extend(roots)
        for r in roots:
            remaining.pop(r)
    return ordered, []


def shared_vconn_tables(facts: list[dict]) -> list[dict]:
    """Group flows by (vconn, table); 2+ readers = stg reuse candidate."""
    readers: dict[tuple, list[str]] = defaultdict(list)
    captions: dict[tuple, tuple[str, str]] = {}
    for f in facts:
        for v in f["inputs_vconn"]:
            key = (v["vconn_luid"], v["table_uuid"])
            if f["name"] not in readers[key]:
                readers[key].append(f["name"])
            captions[key] = (v["vconn_caption"], v["table_name"])
    out = []
    for key, flows in sorted(readers.items(), key=lambda kv: -len(kv[1])):
        if len(flows) < 2:
            continue
        caption, table = captions[key]
        out.append({
            "vconn_caption": caption, "table_name": table,
            "vconn_luid": key[0], "table_uuid": key[1], "flows": flows,
        })
    return out


def render_markdown(facts: list[dict], edges, warnings, order, cycle, shared) -> str:
    from datetime import datetime, timezone
    L: list[str] = []
    L.append("# Flow dependencies")
    L.append("")
    L.append("## Meta")
    L.append(f"- Flows analyzed: {len(facts)}")
    L.append(f"- In-scope dependency edges: {len(edges)}")
    L.append(f"- Generated at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    L.append("")
    L.append("## Per-flow inputs / outputs")
    L.append("")
    L.append("| Flow | Output PDS | Input PDS | Input vconn tables | Other inputs | Incremental |")
    L.append("|---|---|---|---|---|---|")
    for f in facts:
        outs = "<br>".join(o["datasource_name"] or "?" for o in f["outputs"]) or "—"
        ipds = "<br>".join(i["datasource_name"] or "?" for i in f["inputs_pds"]) or "—"
        ivc = "<br>".join(f"{v['vconn_caption']}/{v['table_name']}" for v in f["inputs_vconn"]) or "—"
        ioth = "<br>".join(f"{o['kind']}: {o['node_name']}" for o in f["inputs_other"]) or "—"
        inc = f.get("incremental") or {}
        if inc.get("run_type") == "incremental":
            cf = ", ".join(inc.get("control_fields") or []) or "?"
            incs = f"append (control: {cf})"
        else:
            incs = "—"
        L.append(f"| {f['name']} | {outs} | {ipds} | {ivc} | {ioth} | {incs} |")
    L.append("")
    L.append("## In-scope dependency edges (consumer → producer)")
    L.append("")
    if edges:
        L.append("| Consumer | Producer | Via PDS |")
        L.append("|---|---|---|")
        for c, p, pds in sorted(edges):
            L.append(f"| {c} | {p} | `{pds}` |")
        L.append("")
        L.append("Consumer 側の当該 Input は **暫定 passthrough** 対象 (producer の移行完了後に新 PDS へ差し替える)。")
    else:
        L.append("_(none — all flows are independent)_")
    L.append("")
    L.append("## Topological migration order (producers first)")
    L.append("")
    for i, n in enumerate(order, 1):
        L.append(f"{i}. {n}")
    if cycle:
        L.append("")
        L.append(f"- ⚠️ Cycle detected among: {', '.join(cycle)} (order above is partial)")
    L.append("")
    L.append("## Shared vconn tables (stg reuse candidates)")
    L.append("")
    if shared:
        L.append("| vconn / table | Read by flows |")
        L.append("|---|---|")
        for s in shared:
            L.append(f"| {s['vconn_caption']} / {s['table_name']} | {', '.join(s['flows'])} |")
        L.append("")
        L.append("同一テーブルを読むフロー群は **stg を 1 本に集約** できる (先行セッションの stg PDS を後続が Input 再利用)。")
    else:
        L.append("_(none)_")
    L.append("")
    L.append("## Warnings")
    L.append("")
    if warnings:
        L.extend(f"- ⚠️ {w}" for w in warnings)
    else:
        L.append("_(none)_")
    L.append("")
    return "\n".join(L)


def download_project_flows(project_name: str, download_dir: Path) -> list[tuple[str, Path]]:
    """Download every flow in `project_name` (leaf-name match) as .tfl files.

    Returns (display_name, path) pairs - flow JSON rarely carries a usable
    top-level name, so the server-side display name is threaded through.
    """
    from tableau_auth import signed_in_server
    import tableauserverclient as TSC

    download_dir.mkdir(parents=True, exist_ok=True)
    leaf = project_name.split("/")[-1].strip()
    named_files: list[tuple[str, Path]] = []
    with signed_in_server() as server:
        flows = [f for f in TSC.Pager(server.flows) if f.project_name == leaf]
        if not flows:
            sys.exit(f"ERROR: no flows found in project '{project_name}'")
        for fl in flows:
            base = download_dir / fl.id  # LUID filename avoids name collisions
            got = Path(server.flows.download(fl.id, filepath=str(base)))
            named_files.append((fl.name, got))
            print(f"[map_flow_dependencies] downloaded: {fl.name} -> {got.name}",
                  file=sys.stderr)
    return named_files


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    p.add_argument("flow_paths", nargs="*", type=Path,
                   help="Flow files (.tfl/.tflx/flow.json) and/or directories containing them")
    p.add_argument("--project",
                   help="Server mode: analyze every flow in this project "
                        "(leaf name match, e.g. '1_Prep'). Requires --download-dir.")
    p.add_argument("--download-dir", type=Path,
                   help="Where --project downloads .tfl files (typically scratch/)")
    p.add_argument("-o", "--output", type=Path, required=True,
                   help="flow-dependencies.md output path")
    p.add_argument("--json", type=Path,
                   help="Optionally also write the raw facts/edges as JSON")
    args = p.parse_args()

    if args.project:
        if not args.download_dir:
            p.error("--project requires --download-dir")
        named_files = download_project_flows(args.project, args.download_dir)
    else:
        if not args.flow_paths:
            p.error("give flow_paths, or use --project")
        named_files = [(None, f) for f in collect_flow_files(args.flow_paths)]

    facts = []
    name_seen: set[str] = set()
    for display_name, f in named_files:
        flow = load_flow(f)
        if not isinstance(flow, dict) or not flow.get("nodes"):
            # Not a Prep flow (e.g. an unrelated .json sitting in the dir) - skip
            print(f"[map_flow_dependencies] skipped non-flow file: {f.name}", file=sys.stderr)
            continue
        # Server-side display name wins; else the flow's own name; else the
        # file stem. flow['name'] is often the literal "flow" - skip that.
        name = display_name or flow.get("name")
        if not name or name == "flow":
            name = f.stem
        if name in name_seen:
            name = f"{name} ({f.stem})"
        name_seen.add(name)
        facts.append(extract_flow_facts(name, flow))

    edges, warnings = build_edges(facts)
    order, cycle = topological_order(facts, edges)
    shared = shared_vconn_tables(facts)

    md = render_markdown(facts, edges, warnings, order, cycle, shared)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(md, encoding="utf-8")
    print(f"Wrote {len(md):,} chars to: {args.output}", file=sys.stderr)

    if args.json:
        args.json.write_text(json.dumps({
            "flows": facts, "edges": [list(e) for e in edges],
            "topological_order": order, "cycle": cycle,
            "shared_vconn_tables": shared, "warnings": warnings,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote JSON to: {args.json}", file=sys.stderr)

    print(f"RESULT_JSON: {json.dumps({'flows': len(facts), 'edges': len(edges), 'cycle': bool(cycle), 'shared_vconn_tables': len(shared)})}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
