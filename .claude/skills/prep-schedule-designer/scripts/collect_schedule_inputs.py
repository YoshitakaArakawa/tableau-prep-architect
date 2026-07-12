#!/usr/bin/env python3
"""Collect schedule-design inputs from session manifests + decomposed .tfl files.

Produces `schedule-inputs.json`: the single machine-verified input for schedule
design (Phase B). Never trusts run-type or dependency claims written in plan /
design documents — both are re-derived from the .tfl files themselves:

  - run-type:  flow_io.get_incremental_config() on each .tfl
               (incrementalEnabled + controlFieldName + append output)
  - deps:      LoadSqlProxy inputs' datasourceName matched against the output
               PDS names of the other collected flows (internal edges only;
               inputs reading Live stg PDS or external PDS produce no edge)

Manifests are passed explicitly by the caller (no work/ glob discovery: stale
sessions and superseded manifests would silently pollute the flow set).

Ordering: per weakly-connected component, a deterministic topological sort with
the facts-last policy — intermediate flows and hub marts (marts consumed by
other flows) as early as dependencies allow, leaf marts (consumed by nobody)
last. Components are emitted as suggested schedule domains; the business
grouping/trigger decision stays with the caller.

Usage:
    python collect_schedule_inputs.py \
      --manifest <session>/reports/publish-manifest.json \
      --manifest <other-session>/reports/publish-manifest-xyz.json \
      --out <session>/reports/schedule-inputs.json

Exit code 0 on success (warnings allowed), 1 on any fatal input error.
Final line: RESULT_JSON: {...}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))

from flow_io import get_incremental_config, load_flow_json  # noqa: E402

LOAD_SQL_PROXY_SUFFIX = "LoadSqlProxy"  # nodeType ".v2019_3_1.LoadSqlProxy" etc.


def _fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    print("RESULT_JSON: " + json.dumps({"status": "error", "message": msg}))
    sys.exit(1)


def _resolve_tfl_path(manifest_path: Path, tfl_path: str) -> Path | None:
    """Resolve a manifest-relative tfl_path to an existing file.

    Manifests live in <session>/reports/ and tfl_path values are relative to
    the session root (e.g. "flows/marts/x.tfl"). session_work_dir in the
    manifest is unreliable across machines (may be absolute), so resolution
    is anchored on the manifest file's location.
    """
    candidates = [
        manifest_path.parent.parent / tfl_path,  # <session>/reports/../<tfl_path>
        manifest_path.parent / tfl_path,         # fallback: next to the manifest
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _proxy_inputs(flow: dict[str, Any]) -> list[dict[str, str]]:
    """List LoadSqlProxy inputs as {node_name, project_name, datasource_name}."""
    out = []
    for node in (flow.get("nodes") or {}).values():
        if not str(node.get("nodeType", "")).endswith(LOAD_SQL_PROXY_SUFFIX):
            continue
        attrs = node.get("connectionAttributes") or {}
        out.append({
            "node_name": node.get("name") or "",
            "project_name": attrs.get("projectName") or "",
            "datasource_name": attrs.get("datasourceName") or "",
        })
    return out


def collect(manifest_paths: list[Path]) -> dict[str, Any]:
    flows: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen_names: dict[str, str] = {}  # flow name -> manifest it came from

    for mp in manifest_paths:
        if not mp.exists():
            _fail(f"manifest not found: {mp}")
        manifest = json.loads(mp.read_text(encoding="utf-8"))
        for entry in manifest.get("decomposed_flows", []):
            kind = entry.get("kind")
            if kind == "pds_augment":
                continue  # Live stg PDS: no flow run, never scheduled
            if kind != "tfl":
                warnings.append(f"{mp.name}: unknown kind {kind!r} on {entry.get('name')}, skipped")
                continue
            name = entry.get("name")
            if name in seen_names:
                warnings.append(
                    f"duplicate flow name {name!r} in {mp.name} (already from "
                    f"{seen_names[name]}); keeping the first, verify manifests"
                )
                continue
            seen_names[name] = mp.name

            flow_luid = (entry.get("publish") or {}).get("flow_luid")
            if not flow_luid:
                warnings.append(f"{name}: no flow_luid in manifest (not published?); included without LUID")

            tfl_rel = entry.get("tfl_path")
            tfl_abs = _resolve_tfl_path(mp, tfl_rel) if tfl_rel else None
            if tfl_abs is None:
                _fail(f"{name}: tfl not found (tfl_path={tfl_rel!r}, manifest={mp})")

            flow = load_flow_json(tfl_abs)
            incr = get_incremental_config(flow)
            for c in incr["incremental_configs"]:
                if c["inert"]:
                    warnings.append(
                        f"{name}: inert IncrementalConfiguration on input {c['input_node_id']}"
                        " (enabled but no control field/output) — UI residue, treated as full"
                    )

            flows.append({
                "name": name,
                "layer": entry.get("layer"),
                "flow_luid": flow_luid,
                "manifest": str(mp),
                "outputs": [o.get("name") for o in entry.get("outputs", [])],
                "run_type": incr["run_type"],
                "control_fields": incr["control_fields"],
                "inputs": _proxy_inputs(flow),
            })

    if not flows:
        _fail("no schedulable (kind=tfl) flows found in the given manifests")

    # --- internal dependency edges: input datasource_name -> producing flow ---
    producer_by_output: dict[str, str] = {}
    for f in flows:
        for out_name in f["outputs"]:
            if out_name in producer_by_output:
                warnings.append(f"output name {out_name!r} produced by two flows — edges may be wrong")
            producer_by_output[out_name] = f["name"]

    for f in flows:
        deps = sorted({
            producer_by_output[i["datasource_name"]]
            for i in f["inputs"]
            if i["datasource_name"] in producer_by_output
            and producer_by_output[i["datasource_name"]] != f["name"]
        })
        f["depends_on"] = deps

    consumers: dict[str, list[str]] = {f["name"]: [] for f in flows}
    for f in flows:
        for dep in f["depends_on"]:
            consumers[dep].append(f["name"])
    for f in flows:
        f["consumed_by"] = sorted(consumers[f["name"]])
        f["is_leaf_mart"] = f["layer"] == "marts" and not f["consumed_by"]
        f["is_hub_mart"] = f["layer"] == "marts" and bool(f["consumed_by"])

    # --- weakly connected components = suggested schedule domains ---
    name_to_flow = {f["name"]: f for f in flows}
    comp_of: dict[str, int] = {}
    comp_id = 0
    for f in flows:
        if f["name"] in comp_of:
            continue
        stack = [f["name"]]
        while stack:
            cur = stack.pop()
            if cur in comp_of:
                continue
            comp_of[cur] = comp_id
            cur_f = name_to_flow[cur]
            stack.extend(cur_f["depends_on"])
            stack.extend(cur_f["consumed_by"])
        comp_id += 1

    # --- facts-last topological order within each component ---
    def facts_last_order(members: list[dict[str, Any]]) -> list[str]:
        remaining = {m["name"] for m in members}
        placed: list[str] = []
        while remaining:
            ready = [
                name_to_flow[n] for n in remaining
                if all(d not in remaining for d in name_to_flow[n]["depends_on"])
            ]
            if not ready:  # dependency cycle: bail deterministically
                cycle = sorted(remaining)
                warnings.append(f"dependency cycle among {cycle}; appending in name order")
                placed.extend(cycle)
                break
            # facts-last: among ready nodes prefer non-mart, then hub marts,
            # then leaf marts; ties broken by name for determinism. A leaf
            # mart is only chosen when nothing else is ready.
            def rank(m: dict[str, Any]) -> tuple[int, str]:
                if m["layer"] != "marts":
                    tier = 0
                elif m["is_hub_mart"]:
                    tier = 1
                else:
                    tier = 2
                return (tier, m["name"])
            nxt = min(ready, key=rank)
            placed.append(nxt["name"])
            remaining.discard(nxt["name"])
        return placed

    components = []
    for cid in range(comp_id):
        members = [f for f in flows if comp_of[f["name"]] == cid]
        order = facts_last_order(members)
        for pos, name in enumerate(order, start=1):
            name_to_flow[name]["suggested_order"] = pos
        components.append({
            "component_id": cid,
            "flows_in_order": order,
            "incremental_flows": [m["name"] for m in members if m["run_type"] == "incremental"],
        })

    return {
        "schema_version": "1",
        "flows": flows,
        "components": components,
        "warnings": warnings,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--manifest", action="append", required=True, dest="manifests",
                    help="publish-manifest JSON path (repeatable)")
    ap.add_argument("--out", required=True, help="output path for schedule-inputs.json")
    args = ap.parse_args()

    result = collect([Path(m) for m in args.manifests])

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    n_incr = sum(1 for f in result["flows"] if f["run_type"] == "incremental")
    print(f"collected {len(result['flows'])} flows "
          f"({n_incr} incremental) into {len(result['components'])} components -> {out}")
    for w in result["warnings"]:
        print(f"WARNING: {w}", file=sys.stderr)
    print("RESULT_JSON: " + json.dumps({
        "status": "ok",
        "out": str(out),
        "flows": len(result["flows"]),
        "incremental": n_incr,
        "components": len(result["components"]),
        "warnings": len(result["warnings"]),
    }))


if __name__ == "__main__":
    main()
