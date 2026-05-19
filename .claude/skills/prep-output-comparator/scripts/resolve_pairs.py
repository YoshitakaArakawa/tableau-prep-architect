"""Resolve original ↔ new published-datasource pairs via Tableau Metadata API.

Given:
  - original flow LUID (1 flow that the new flows are decomposed from)
  - new flow LUIDs (typically the marts-layer .tfl flows after decomposition)

Looks up each flow's downstream published datasources via the Metadata API
(GraphQL) and writes a pairs.json with the original outputs paired against
the flattened new outputs (paired by position).

Usage:

    python resolve_pairs.py \
        --original-flow-luid <luid> \
        --new-flow-luids <luid1> <luid2> ... \
        --output <output_dir>/pairs.json

Failure modes:
  - Original or new flow not found → exits with error
  - Flow has zero downstream datasources → reported as warning, written to JSON
  - Original output count != new flattened output count → warning, still pairs
    by index for the shorter length
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# This file lives at .claude/skills/prep-output-comparator/scripts/resolve_pairs.py
# → parents[4] is the repo root (4 levels up: scripts → prep-output-comparator → skills → .claude → repo)
REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from tableau_auth import sign_in_server  # noqa: E402


# Metadata API uses "publishedDatasources" with upstreamFlows for reverse lookup.
# Forward lookup from Flow → downstreamDatasources is the documented path.
FLOW_OUTPUTS_QUERY = """
query FlowDownstreamDatasources($luid: String!) {
  flows(filter: { luid: $luid }) {
    luid
    name
    downstreamDatasources {
      luid
      name
      projectName
    }
  }
}
"""


def query_flow_outputs(server, flow_luid: str) -> dict[str, Any]:
    """Return the flow record with its downstream published datasources.

    Returns a dict with keys: luid, name, downstreamDatasources (list).
    """
    result = server.metadata.query(
        query=FLOW_OUTPUTS_QUERY,
        variables={"luid": flow_luid},
    )

    if "errors" in result and result["errors"]:
        msgs = "; ".join(e.get("message", "?") for e in result["errors"])
        sys.exit(f"ERROR: Metadata API returned errors for flow {flow_luid}: {msgs}")

    flows = result.get("data", {}).get("flows", [])
    if not flows:
        sys.exit(f"ERROR: No flow found with LUID: {flow_luid}")

    return flows[0]


def jst_now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--original-flow-luid", required=True,
                        help="LUID of the original flow")
    parser.add_argument("--new-flow-luids", required=True, nargs="+",
                        help="LUIDs of the new decomposed flows (typically marts layer)")
    parser.add_argument("--output", required=True,
                        help="Path to write the pairs.json")
    args = parser.parse_args()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    server, auth = sign_in_server()
    warnings: list[str] = []

    with server.auth.sign_in(auth):
        # 1. Resolve original flow's outputs
        orig_flow = query_flow_outputs(server, args.original_flow_luid)
        original_outputs = orig_flow.get("downstreamDatasources") or []
        if not original_outputs:
            warnings.append(
                f"Original flow '{orig_flow.get('name')}' ({args.original_flow_luid}) "
                "has no downstream datasources"
            )

        # 2. Resolve each new flow's outputs (sequential — avoids parallel auth races)
        new_outputs_by_flow: dict[str, dict[str, Any]] = {}
        for new_luid in args.new_flow_luids:
            new_flow = query_flow_outputs(server, new_luid)
            outs = new_flow.get("downstreamDatasources") or []
            if not outs:
                warnings.append(
                    f"New flow '{new_flow.get('name')}' ({new_luid}) "
                    "has no downstream datasources"
                )
            new_outputs_by_flow[new_luid] = {
                "name": new_flow.get("name"),
                "outputs": outs,
            }

    # 3. Flatten new outputs in the order new_flow_luids was given
    flat_new: list[dict[str, Any]] = []
    for new_luid in args.new_flow_luids:
        for ds in new_outputs_by_flow[new_luid]["outputs"]:
            flat_new.append({**ds, "source_flow_luid": new_luid})

    # 4. Pair by index
    if len(original_outputs) != len(flat_new):
        warnings.append(
            f"Output count mismatch: original={len(original_outputs)}, "
            f"new(flattened)={len(flat_new)}. Pairing by index up to "
            f"the shorter length ({min(len(original_outputs), len(flat_new))})."
        )

    pair_count = min(len(original_outputs), len(flat_new))
    pairs = []
    for i in range(pair_count):
        orig = original_outputs[i]
        new = flat_new[i]
        pairs.append({
            "pair_index": i,
            "original": {
                "luid": orig.get("luid"),
                "name": orig.get("name"),
                "project_name": orig.get("projectName"),
            },
            "new": {
                "luid": new.get("luid"),
                "name": new.get("name"),
                "project_name": new.get("projectName"),
                "source_flow_luid": new.get("source_flow_luid"),
            },
        })

    payload = {
        "schema_version": "1",
        "generated_at": jst_now_iso(),
        "original_flow_luid": args.original_flow_luid,
        "new_flow_luids": args.new_flow_luids,
        "pairs": pairs,
        "warnings": warnings,
    }

    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[resolve_pairs] Wrote {pair_count} pair(s) to {output_path}", file=sys.stderr)
    if warnings:
        for w in warnings:
            print(f"[resolve_pairs] WARNING: {w}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
