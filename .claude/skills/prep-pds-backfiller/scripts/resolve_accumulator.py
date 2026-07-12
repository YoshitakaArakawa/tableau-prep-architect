#!/usr/bin/env python3
"""Resolve, from a deployed Prep flow, which published output is the incremental
accumulator that a backfill must target.

Ground truth = the live flow JSON (download the .tfl by LUID), NOT planning docs
or manifests, which drift when flows are renamed / delete+republished. An
accumulator is an output whose flow co-resides an incremental Input
(IncrementalConfiguration with a control field) AND an APPEND output
(OutputRefreshOptions.outputOperationType == append). Backfilling a full-refresh
/ replace output is unsafe: the next run overwrites the seeded history.

Resolution is per OUTPUT node (multi-output flows can hold an append accumulator
AND a full-refresh mirror at once). Each output is classified:
  - accumulator  : append output wired to an ENABLED IncrementalConfiguration
  - inert_incr   : IncrementalConfiguration present but controlFieldName /
                   outputNodeId empty -> Prep ignores it (treat as full-refresh)
  - append_only  : append output with no incremental wiring (unusual; flagged)
  - full_refresh : replace / create output (mirror; do NOT backfill)

For each accumulator the published PDS name + project are read from the output
node, and the PDS LUID is resolved by name+project (best effort) so the caller
gets the exact target LUID for backfill_pds.py.

Usage:
  python resolve_accumulator.py --flow-luid <luid> [--flow-luid <luid> ...]
  python resolve_accumulator.py --flow-luid <luid> --out accumulators.json

Cloud access is read-only (flow download + datasource lookup).
Final line: RESULT_JSON: {...}
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "scripts"))

import tableauserverclient as TSC  # noqa: E402

from flow_io import load_flow_json  # noqa: E402
from tableau_auth import signed_in_server  # noqa: E402

# Substring keys, matched case-sensitively against the fully-qualified
# nodeProperties class names. Substring (not exact) so a version bump of the
# serializer class (…v2020_2_1.IncrementalConfiguration -> …v2021_x…) still
# matches; flow_io pins one version for WRITING, this reader must tolerate any.
INCR_KEY = "IncrementalConfiguration"
OUTPUT_REFRESH_KEY = "OutputRefreshOptions"
APPEND_OP = "outputOperationTypeAppend"


def _find_prop(props_for_node: dict | None, needle: str) -> dict | None:
    """Return the first nodeProperties value whose key contains `needle`."""
    for k, v in (props_for_node or {}).items():
        if needle in k and isinstance(v, dict):
            return v
    return None


def _is_output(node: dict) -> bool:
    if node.get("baseType") == "output":
        return True
    ntype = str(node.get("nodeType", ""))
    base = ntype.rsplit(".", 1)[-1].lower()
    return "output" in base or "publishextract" in base


def classify_outputs(flow: dict) -> list[dict]:
    """Classify every output node in one flow. See module docstring for labels."""
    nodes = flow.get("nodes") or {}
    node_props = flow.get("nodeProperties") or {}

    # Index incremental configs by the output node they target.
    incr_by_output: dict[str, dict] = {}
    for nid, props in node_props.items():
        cfg = _find_prop(props, INCR_KEY)
        if cfg is None:
            continue
        control = cfg.get("controlFieldName") or ""
        out_id = cfg.get("outputNodeId") or ""
        enabled = bool(cfg.get("incrementalEnabled")) and bool(control) and bool(out_id)
        # Index by out_id when known; otherwise keep under the input id so an
        # inert config on a lone input is still surfaced.
        key = out_id or nid
        incr_by_output[key] = {
            "input_node_id": nid,
            "control_field": control,
            "output_field": cfg.get("outputFieldName") or "",
            "enabled": enabled,
        }

    results = []
    for nid, node in nodes.items():
        if not _is_output(node):
            continue
        refresh = _find_prop(node_props.get(nid), OUTPUT_REFRESH_KEY)
        op = (refresh or {}).get("outputOperationType", "") or ""
        is_append = op == APPEND_OP
        incr = incr_by_output.get(nid)

        if incr and incr["enabled"] and is_append:
            classification = "accumulator"
        elif incr and not incr["enabled"]:
            classification = "inert_incr"
        elif is_append:
            classification = "append_only"
        else:
            classification = "full_refresh"

        results.append({
            "output_node_id": nid,
            "output_node_name": node.get("name"),
            "datasource_name": node.get("datasourceName"),
            "project_name": node.get("projectName"),
            "project_luid": node.get("projectLuid"),
            "operation": op or "replace/create",
            "control_field": (incr or {}).get("control_field") or None,
            "classification": classification,
        })
    return results


def resolve_pds_luid(server, name: str | None, project_luid: str | None) -> str | None:
    """Best-effort: find the published datasource LUID by name (+ project)."""
    if not name:
        return None
    req = TSC.RequestOptions()
    req.filter.add(TSC.Filter(TSC.RequestOptions.Field.Name,
                              TSC.RequestOptions.Operator.Equals, name))
    matches, _ = server.datasources.get(req)
    if project_luid:
        scoped = [d for d in matches if d.project_id == project_luid]
        if scoped:
            matches = scoped
    if len(matches) == 1:
        return matches[0].id
    return None  # 0 or ambiguous -> leave for the caller to disambiguate


def inspect(server, flow_luid: str, workdir: Path) -> dict:
    dest = workdir / flow_luid
    dest.mkdir(parents=True, exist_ok=True)
    tfl = Path(server.flows.download(flow_luid, filepath=str(dest)))
    flow = load_flow_json(tfl)
    outputs = classify_outputs(flow)

    accumulators = [o for o in outputs if o["classification"] == "accumulator"]
    for acc in accumulators:
        acc["pds_luid"] = resolve_pds_luid(
            server, acc.get("datasource_name"), acc.get("project_luid"))

    return {
        "flow_luid": flow_luid,
        "output_count": len(outputs),
        "accumulators": accumulators,
        "other_outputs": [o for o in outputs if o["classification"] != "accumulator"],
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--flow-luid", action="append", required=True,
                    help="deployed flow LUID (repeatable)")
    ap.add_argument("--out", help="write the full report JSON here")
    ap.add_argument("--workdir", help="dir for transient .tfl downloads "
                    "(default: a system temp dir, kept for inspection)")
    args = ap.parse_args(argv)

    workdir = Path(args.workdir) if args.workdir else \
        Path(tempfile.mkdtemp(prefix="resolve_acc_"))
    report = {"schema_version": "1", "flows": []}
    errors = []
    with signed_in_server() as server:
        for luid in args.flow_luid:
            print(f"\n===== flow {luid} =====")
            # A stale / deleted flow LUID (404) must not abort resolution of the
            # rest of the batch -- record it and move on.
            try:
                entry = inspect(server, luid, workdir)
            except Exception as e:
                lines = [ln.strip() for ln in str(e).splitlines() if ln.strip()]
                msg = lines[-1] if lines else repr(e)
                print(f"  ERROR: {msg}")
                errors.append({"flow_luid": luid, "error": msg})
                report["flows"].append({"flow_luid": luid, "error": msg,
                                        "accumulators": [], "other_outputs": []})
                continue
            report["flows"].append(entry)
            for acc in entry["accumulators"]:
                print(f"  [ACCUMULATOR] {acc['datasource_name']!r} "
                      f"control={acc['control_field']!r} pds_luid={acc.get('pds_luid')}")
            for o in entry["other_outputs"]:
                print(f"  [{o['classification']}] {o['datasource_name']!r} op={o['operation']}")

    payload = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload, encoding="utf-8")
        print(f"\nreport -> {out}")

    n_acc = sum(len(f["accumulators"]) for f in report["flows"])
    unresolved = sum(1 for f in report["flows"] for a in f["accumulators"]
                     if not a.get("pds_luid"))
    print("RESULT_JSON: " + json.dumps({
        "status": "ok" if not errors else "partial",
        "flows": len(report["flows"]),
        "flow_errors": errors,
        "accumulators": n_acc,
        "unresolved_luids": unresolved,
        "out": args.out,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
