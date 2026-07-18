"""Shared helpers for tableau-prep-builder per-session build scripts.

Promotes the small set of utilities that every `build_tfls.py` previously
re-implemented at the top of the file (`empty_flow`, `reset_next_nodes`,
`add_edge`, `split_supertransform_actions`, `transplant_source_input`)
into one place. The session-specific build script then only contains the
per-.tfl topology, not the boilerplate.

flow_io.py remains the lower-level primitives layer (`copy_source_node`,
`add_pds_input`, `make_publish_extract_node`, verifiers, zip pack). This
module sits between flow_io and the session script.

Typical usage (compare to a self-contained build_tfls.py — these are the
only helpers that get pulled out; everything else stays in the session
script):

    from build_helpers import (
        empty_flow,
        reset_next_nodes,
        add_edge,
        split_supertransform_actions,
        transplant_source_input,
    )

    flow = empty_flow("stg_internal__transactions")
    inp_id = transplant_source_input(flow, src, N_TRANSACTIONS_ID)
    clean_split = split_supertransform_actions(
        src["nodes"][N_CLEAN1], [0, 1, 2, 3],
        new_name="Clean 1 (stg renames)", new_id=str(uuid.uuid4()),
    )
    flow["nodes"][clean_split["id"]] = clean_split
    reset_next_nodes(flow["nodes"][inp_id])
    add_edge(flow["nodes"][inp_id], clean_split["id"])
    # ... PublishExtract via flow_io.make_publish_extract_node ...

The helpers deliberately stay as functions (not a class) so the session
script can mix and match them freely with the flow_io primitives that
already encapsulate the trickier operations (`copy_source_node` with
`kept_children=`, namespace-preserving `wire_new_input_to_child`).
"""

from __future__ import annotations

import copy
import uuid
from typing import Any


def empty_flow(name: str) -> dict[str, Any]:
    """Skeleton flow dict mirroring the top-level shape of a Prep .tfl flow JSON.

    Caller fills in `nodes`, `connections`, `dataConnections`, `initialNodes`
    via the flow_io primitives or the helpers in this module. `version` /
    `loomVersion` follow Prep 2025.3.x conventions; the maestroMetadata aux
    entry carried over by pack_flow_json overrides the surface version
    detection on publish anyway, but keeping these synced reduces noise.
    """
    return {
        "name": name,
        "version": "2025.3.0",
        "loomVersion": "23.0",
        "documentId": str(uuid.uuid4()),
        "majorVersion": 1,
        "minorVersion": 8,
        "nodes": {},
        "connections": {},
        "connectionIds": [],
        "dataConnections": {},
        "dataConnectionIds": [],
        "initialNodes": [],
        "extensibility": {},
        "obfuscatorId": str(uuid.uuid4()),
        "parameters": {"parameters": {}},
        "selection": [],
        "nodeProperties": {},
    }


def reset_next_nodes(node: dict[str, Any]) -> None:
    """Clear `nextNodes` on a node so the caller can re-wire from scratch.

    Use after `copy_source_node` if the inherited child references aren't
    relevant to the new flow's topology and the caller will add edges
    explicitly via `add_edge` / `flow_io.wire_new_input_to_child`.
    """
    node["nextNodes"] = []


def add_edge(parent: dict[str, Any], child_id: str, *, ns: str = "Default") -> None:
    """Append a single nextNodes edge on `parent` pointing to `child_id`.

    `ns` is the `nextNamespace` on the edge. For Union/Join children inherit
    the source flow's namespace verbatim (`Union-Namespace-<hex>` / `Left` /
    `Right`); for plain linear chains `"Default"` is correct. NEVER let a
    Union/Join edge fall through to Default unintentionally — that silently
    breaks the input identity mapping at run time.
    """
    parent.setdefault("nextNodes", []).append({
        "namespace": "Default",
        "nextNodeId": child_id,
        "nextNamespace": ns,
    })


def split_supertransform_actions(
    src_node: dict[str, Any],
    action_indices: list[int],
    *,
    new_name: str,
    new_id: str,
) -> dict[str, Any]:
    """Deep-copy a SuperTransform, keep only the chosen `beforeActionAnnotations`.

    Used when one source SuperTransform's actions span layers (e.g. Clean 2's
    ChangeType + Rename actions belong to stg, FIXED MAX + Filter belong to
    int — same source node, split across two new .tfl files). The retained
    actions keep their original order.

    The source's inner-action chain (`annotationNode.nextNodes`) is preserved
    verbatim on the kept slice; the runner walks the list sequentially, so
    the chain breaking at the trim point is tolerated.
    """
    new = copy.deepcopy(src_node)
    new["id"] = new_id
    new["name"] = new_name
    all_actions = new.get("beforeActionAnnotations") or []
    new["beforeActionAnnotations"] = [all_actions[i] for i in action_indices]
    new["nextNodes"] = []
    return new


def transplant_source_input(
    new_flow: dict[str, Any],
    source_flow: dict[str, Any],
    source_input_node_id: str,
) -> str:
    """Copy a source-flow Input node + its connection + dataConnection verbatim.

    Use for non-LSP Input nodes (e.g. a `LoadSql` against a virtual
    connection where the source's `publishedConnection` cannot be replaced
    by an `add_pds_input` LSP). For PDS inputs (cross-layer reads) use
    `flow_io.add_pds_input` instead — it builds a fresh LSP with the
    correct Server connection + dataConnection.

    Returns the new node ID (same UUID as in source; node IDs are kept
    stable for edge wiring).
    """
    src_input = source_flow["nodes"][source_input_node_id]
    new_input = copy.deepcopy(src_input)
    new_input["nextNodes"] = []
    new_flow["nodes"][new_input["id"]] = new_input

    conn_id = new_input.get("connectionId")
    if not conn_id:
        return new_input["id"]

    def _copy_connection(cid: str) -> None:
        conn = (source_flow.get("connections") or {}).get(cid)
        if conn is not None:
            new_flow.setdefault("connections", {})[cid] = copy.deepcopy(conn)
            if cid not in new_flow.setdefault("connectionIds", []):
                new_flow["connectionIds"].append(cid)

    dconn = (source_flow.get("dataConnections") or {}).get(conn_id)
    if dconn is not None:
        # wrapped variant: connectionId -> dataConnection -> baseConnection
        new_flow.setdefault("dataConnections", {})[conn_id] = copy.deepcopy(dconn)
        if conn_id not in new_flow.setdefault("dataConnectionIds", []):
            new_flow["dataConnectionIds"].append(conn_id)
        base_id = dconn.get("baseConnectionId")
        if base_id:
            _copy_connection(base_id)
    else:
        # direct variant: connectionId points straight into connections
        _copy_connection(conn_id)

    return new_input["id"]


__all__ = [
    "empty_flow",
    "reset_next_nodes",
    "add_edge",
    "split_supertransform_actions",
    "transplant_source_input",
]
