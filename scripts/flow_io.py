"""Shared .tfl / .tflx I/O helpers.

Tableau Prep flow files are zip archives. A real .tfl produced by Prep Builder
contains multiple entries — at minimum:

    flow              — flow definition JSON (required)
    maestroMetadata   — internal "Maestro" metadata (REQUIRED by Tableau Server
                        publish endpoint; missing it triggers errorCode=280003
                        "Problem reading the provided Flow file")
    displaySettings   — UI layout / pane state (not strictly required by publish,
                        but Prep Builder expects it for a normal open experience)
    flowGraphImage.png, flowGraphThumbnail.svg
                      — preview images (cosmetic, can be omitted)

Skipping `maestroMetadata` produces a .tfl that Prep CLI refuses to load
("InvalidMaestroDocumentMetadataNotFoundMsg") AND that the publish REST endpoint
rejects with errorCode=280003. So any builder that wants its output to publish
MUST carry `maestroMetadata` (and ideally `displaySettings`) from the source.

Usage:

    from flow_io import (
        load_flow_json, load_aux_entries,
        unpack_flow_json, pack_flow_json,
    )

    flow = load_flow_json("source.tfl")
    aux  = load_aux_entries("source.tfl")            # {"maestroMetadata": b"...", ...}

    pack_flow_json(new_flow, "flows/staging/stg_orders.tfl",
                   aux_entries={k: aux[k] for k in ("maestroMetadata", "displaySettings")
                                if k in aux})
"""

from __future__ import annotations

import copy
import json
import uuid
import zipfile
from pathlib import Path
from typing import Any

# Entries that must be carried over from the source .tfl for the new .tfl to
# publish successfully to Tableau Server / Cloud.
PUBLISHABLE_AUX_ENTRIES: tuple[str, ...] = ("maestroMetadata", "displaySettings")

# nodeType strings used when building Inputs / Outputs that reference Tableau
# Server Published Data Sources (the only Input/Output pattern that works for
# cross-layer chaining on Tableau Cloud).
NODE_TYPE_LOAD_SQL_PROXY = ".v2019_3_1.LoadSqlProxy"
NODE_TYPE_PUBLISH_EXTRACT = ".v1.PublishExtract"


def load_flow_json(tfl_path: str | Path) -> dict[str, Any]:
    """Read flow JSON from a .tfl/.tflx without extracting to disk."""
    with zipfile.ZipFile(tfl_path) as z:
        with z.open("flow") as f:
            return json.load(f)


def load_aux_entries(
    tfl_path: str | Path,
    names: tuple[str, ...] | None = None,
) -> dict[str, bytes]:
    """Read non-flow zip entries from a .tfl/.tflx as raw bytes.

    Pass `names` to restrict which entries are read (e.g. only `maestroMetadata`).
    By default returns every entry except `flow`.
    """
    out: dict[str, bytes] = {}
    with zipfile.ZipFile(tfl_path) as z:
        for info in z.infolist():
            if info.filename == "flow":
                continue
            if names is not None and info.filename not in names:
                continue
            out[info.filename] = z.read(info.filename)
    return out


def unpack_flow_json(tfl_path: str | Path, out_path: str | Path) -> Path:
    """Extract the 'flow' entry of a .tfl/.tflx to a JSON file on disk.

    Creates parent directories as needed. Returns the output path.
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(tfl_path) as z:
        with z.open("flow") as src:
            out.write_bytes(src.read())
    return out


def bfs_order(flow: dict[str, Any]) -> list[str]:
    """Short-ID ordering: BFS from initialNodes, then any unreached nodes.

    This is THE canonical step numbering for the whole toolchain: the
    flow-summary Topology table's `#` column (gen_flow_summary.py) and
    plan.json's step references (plan_model.py / build_from_plan.py) both
    derive from this function, so a step index written at decompose time
    resolves to the same node UUID at build time.
    """
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


def inspect_input_node(flow: dict[str, Any], node_id: str) -> dict[str, Any]:
    """Classify a Prep flow Input node by its upstream connection type.

    Returns a dict with at minimum `{"kind": <category>}`. Categories:
      - "pds"        : Tableau Published Data Source (LoadSqlProxy)
      - "vconn"      : Tableau virtual connection (LoadSql + base
                       connection.class == 'publishedConnection'). Extra keys:
                       vconn_luid, vconn_caption, table_uuid, table_name, fields
      - "direct_db"  : LoadSql against a direct database (Snowflake, Postgres,
                       etc.) - base connection.class is the db driver
      - "extract"    : LoadHyper or similar local-extract loader
      - "unknown"    : nodeType not recognized or node not present / not input

    Used by prep-builder to dispatch stg materialization: vconn -> generate a
    prep-pds-augmenter spec; pds/direct_db/extract -> currently unsupported for
    the live-PDS path (build the .tfl as before, or skip with warning per
    layer-responsibilities.md).

    Detection is decisive (no fuzzy matching): vconn requires both
    nodeType=='.v1.LoadSql' AND base connection class=='publishedConnection',
    plus a parseable `relation.table` of the form '[<uuid>].[<name>]'.

    Two connection-graph serialization variants are supported:
      - wrapped: connectionId -> dataConnections[id] -> baseConnectionId ->
        connections[base] (the base carries class + attributes)
      - direct: connectionId -> connections[id] (dataConnections empty; the
        connection itself carries class + attributes)
    """
    node = (flow.get("nodes") or {}).get(node_id)
    if not node or node.get("baseType") != "input":
        return {"kind": "unknown", "reason": "node missing or not an input"}

    node_type = node.get("nodeType", "")
    if node_type.endswith(".LoadSqlProxy"):
        return {"kind": "pds", "node_type": node_type}
    if node_type.endswith(".LoadHyper"):
        return {"kind": "extract", "node_type": node_type}
    if not node_type.endswith(".LoadSql"):
        return {"kind": "unknown", "node_type": node_type}

    # LoadSql: could be vconn OR direct DB. Resolve the "effective connection"
    # (the one carrying connectionAttributes.class) across both serializations.
    conn_id = node.get("connectionId")
    dconn = (flow.get("dataConnections") or {}).get(conn_id) if conn_id else None
    if dconn:  # wrapped variant: hop through the dataConnection to its base
        base_conn_id = dconn.get("baseConnectionId")
        base_conn = (flow.get("connections") or {}).get(base_conn_id) if base_conn_id else None
        if not base_conn:
            return {"kind": "unknown", "node_type": node_type, "reason": "base connection missing"}
    else:  # direct variant: connectionId points straight into connections
        base_conn = (flow.get("connections") or {}).get(conn_id) if conn_id else None
        if not base_conn:
            return {"kind": "unknown", "node_type": node_type, "reason": "connection missing"}

    base_class = (base_conn.get("connectionAttributes") or {}).get("class")
    if base_class != "publishedConnection":
        return {"kind": "direct_db", "node_type": node_type, "connection_class": base_class}

    # vconn path. Extract identifiers needed to build an augmenter spec.
    base_attrs = base_conn.get("connectionAttributes") or {}
    relation = node.get("relation") or {}
    table_ref = relation.get("table", "")
    table_uuid = None
    table_name = None
    if table_ref.startswith("[") and "].[" in table_ref and table_ref.endswith("]"):
        inner = table_ref[1:-1]  # strip outer brackets
        sep = inner.find("].[")
        if sep > 0:
            table_uuid = inner[:sep]
            table_name = inner[sep + 3:]

    return {
        "kind": "vconn",
        "node_type": node_type,
        "vconn_luid": base_attrs.get("resourceId"),
        "vconn_caption": base_attrs.get("resourceName") or base_conn.get("name"),
        "table_uuid": table_uuid,
        "table_name": table_name,
        "fields": node.get("fields") or [],
    }


def vconn_input_to_augmenter_columns(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate an Input node's `fields[]` into prep-pds-augmenter
    `source.columns[]` entries.

    Input field shape (from flow.json):
        {"name": "<uuid>", "type": "string", "caption": "<display>", ...}
    Output shape (matches spec.source.columns[] for kind=vconn):
        {"name": "[<uuid>]", "remote_name": "<uuid>", "caption": "<display>", "datatype": "<dt>"}

    `isGenerated=True` fields (e.g. Tableau-injected `Table Names` in Union
    outputs) are skipped since they do not exist in the underlying vconn table.
    """
    cols = []
    for f in fields:
        if f.get("isGenerated"):
            continue
        raw_name = f.get("name")
        if not raw_name:
            continue
        cols.append({
            "name": f"[{raw_name}]",
            "remote_name": raw_name,
            "caption": f.get("caption") or raw_name,
            "datatype": f.get("type") or "string",
        })
    return cols


def _ensure_collections(flow: dict[str, Any]) -> None:
    """Ensure the 4 top-level collections that LoadSqlProxy depends on exist."""
    flow.setdefault("connections", {})
    flow.setdefault("dataConnections", {})
    flow.setdefault("connectionIds", [])
    flow.setdefault("dataConnectionIds", [])


def register_server_connection(
    flow: dict[str, Any],
    *,
    server_url: str,
    site_url_name: str,
) -> str:
    """Add (or reuse) a Tableau Server sqlproxy `connections` entry.

    Returns the connection id (uuid). Idempotent on (server_url, site_url_name)
    so multiple LoadSqlProxy nodes that read PDSes from the same Server share
    one connection entry — duplicate Server connections trigger publish
    errorCode=280003 (Salesforce KB 005232681).
    """
    _ensure_collections(flow)
    # Normalize: strip trailing slash so equality is stable.
    server_url = server_url.rstrip("/")
    for conn_id, conn in flow["connections"].items():
        attrs = conn.get("connectionAttributes", {})
        if (
            attrs.get("class") == "sqlproxy"
            and attrs.get("server", "").rstrip("/") == server_url
            and attrs.get("siteUrlName") == site_url_name
        ):
            return conn_id
    conn_id = str(uuid.uuid4())
    flow["connections"][conn_id] = {
        "connectionType": ".v1.SqlConnection",
        "id": conn_id,
        "name": f"{server_url} ({site_url_name})",
        "isPackaged": False,
        "connectionAttributes": {
            "server": server_url,
            "port": "443",
            "query-category": "Data",
            "siteUrlName": site_url_name,
            "channel": "https",
            "class": "sqlproxy",
            "directory": "/dataserver",
            "odbc-native-protocol": "yes",
        },
    }
    flow["connectionIds"].append(conn_id)
    return conn_id


def _default_placeholder_dbname(datasource_name: str) -> str:
    """Placeholder dbname used when the upstream PDS hasn't been published yet.

    Empirically (20260520): the publish endpoint requires `dbname` to be set on
    both LoadSqlProxy `connectionAttributes` and dataConnection
    `modifiedConnectionAttributes`. An obviously-fake string is accepted at
    publish time (errorCode=280003 only fires when the field is absent), but
    flow run will fail until prep-deployer's `discover_pds_dbname.py` patches
    it with the real Cloud-assigned name.
    """
    return f"{datasource_name}_placeholder"


def register_pds_data_connection(
    flow: dict[str, Any],
    *,
    base_connection_id: str,
    project_name: str,
    datasource_name: str,
    dbname: str | None = None,
) -> str:
    """Add a `dataConnections` entry that points to one specific PDS.

    `dbname` is the physical Hyper name Tableau Cloud assigns when the PDS is
    first published (`<datasource_name>_<17-digit-suffix>`). Pass `None` at
    build time if the upstream PDS has not been published yet — a placeholder
    is inserted so publish succeeds; resolve and patch via prep-deployer's
    `discover_pds_dbname.py` after the upstream run completes.

    Returns the dataConnection id (uuid). Each call creates a fresh entry —
    there is no dedup because the same PDS may legitimately appear twice
    (e.g. self-join via two Input nodes).
    """
    _ensure_collections(flow)
    if base_connection_id not in flow["connections"]:
        raise ValueError(
            f"base_connection_id {base_connection_id} not registered in flow['connections']"
        )
    base_name = flow["connections"][base_connection_id]["name"]
    dconn_id = str(uuid.uuid4())
    flow["dataConnections"][dconn_id] = {
        "connectionType": ".QueryDataConnection",
        "id": dconn_id,
        "name": base_name,
        "isPackaged": False,
        "baseConnectionId": base_connection_id,
        "modifiedConnectionAttributes": {
            "dbname": dbname if dbname is not None else _default_placeholder_dbname(datasource_name),
            "projectName": project_name,
            "datasourceName": datasource_name,
        },
    }
    flow["dataConnectionIds"].append(dconn_id)
    return dconn_id


def make_load_sql_proxy_node(
    *,
    data_connection_id: str,
    project_name: str,
    datasource_name: str,
    dbname: str | None = None,
    fields: list[dict[str, Any]] | None = None,
    name: str | None = None,
    node_id: str | None = None,
    next_nodes: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build a LoadSqlProxy node dict ready to be inserted into `flow['nodes']`.

    `data_connection_id` must come from a prior `register_pds_data_connection`
    call. `dbname` is best-effort at build time (see register_pds_data_connection).
    `fields` is the schema (list of {name,type,...}); pass empty list and let
    Tableau Server fill at first run if unknown.
    """
    nid = node_id or str(uuid.uuid4())
    attrs: dict[str, Any] = {
        "dbname": dbname if dbname is not None else _default_placeholder_dbname(datasource_name),
        "projectName": project_name,
        "datasourceName": datasource_name,
    }
    return {
        "nodeType": NODE_TYPE_LOAD_SQL_PROXY,
        "id": nid,
        "name": name or f"{datasource_name} ({project_name})",
        "baseType": "input",
        "nextNodes": next_nodes or [],
        "serialize": False,
        "description": None,
        "connectionId": data_connection_id,
        "connectionAttributes": attrs,
        "fields": fields or [],
        # Defaults observed in real .tfl files. Publish validation may require
        # these even when empty — `relation` in particular tells Prep which
        # table to read from the connection.
        "actions": [],
        "debugModeRowLimit": 393216,
        "originalDataTypes": {},
        "randomSampling": None,
        "updateTimestamp": 0,
        "restrictedFields": {},
        "userRenamedFields": {},
        "selectedFields": None,
        "samplingType": None,
        "groupByFields": None,
        "filters": [],
        "relation": {"type": "table", "table": "[sqlproxy]"},
    }


def make_publish_extract_node(
    *,
    project_name: str,
    project_luid: str,
    datasource_name: str,
    server_url: str,
    site_url_name: str,
    name: str = "Output",
    description: str = "",
    node_id: str | None = None,
) -> dict[str, Any]:
    """Build a PublishExtract Output node dict.

    `serverUrl` is composed as `<server_url>/#/site/<site_url_name>` to match the
    canonical shape Prep Builder emits. `project_luid` is required so the publish
    target is unambiguous; obtain it from prep-extractor's deploy-context.md.
    """
    nid = node_id or str(uuid.uuid4())
    site_part = f"/#/site/{site_url_name}" if site_url_name else ""
    return {
        "nodeType": NODE_TYPE_PUBLISH_EXTRACT,
        "id": nid,
        "name": name,
        "baseType": "output",
        "nextNodes": [],
        "serialize": False,
        "description": None,
        "projectName": project_name,
        "projectLuid": project_luid,
        "datasourceName": datasource_name,
        "datasourceDescription": description,
        "serverUrl": f"{server_url.rstrip('/')}{site_part}",
    }


def make_rename_supertransform(
    *,
    renames: list[tuple[str, str]],
    name: str = "Rename-back",
    node_id: str | None = None,
) -> dict[str, Any]:
    """Build a SuperTransform whose actions are RenameColumn (old -> new), in order.

    Primary use: the mart-boundary presentation rename ("rename-back").
    Decomposed mart outputs must reproduce the original output PDS schema
    including column names; engineering names introduced in stg/int are
    renamed back to the original names in a dedicated node inserted just
    before the PublishExtract Output (appending actions to an existing node
    risks the column-drop ordering trap, so a separate node is safer).

    The inner annotation chain is wired sequentially; the runner walks the
    list in order.
    """
    nid = node_id or str(uuid.uuid4())
    ann_ids = [str(uuid.uuid4()) for _ in renames]
    annotations: list[dict[str, Any]] = []
    for i, (old, new) in enumerate(renames):
        nxt = (
            [{"namespace": "Default", "nextNodeId": ann_ids[i + 1], "nextNamespace": "Default"}]
            if i + 1 < len(renames)
            else []
        )
        annotations.append({
            "namespace": "Default",
            "annotationNode": {
                "nodeType": ".v1.RenameColumn",
                "columnName": old,
                "rename": new,
                "name": f"renamed {old} to {new}",
                "id": ann_ids[i],
                "baseType": "transform",
                "nextNodes": nxt,
                "serialize": False,
                "description": None,
            },
        })
    return {
        "nodeType": ".v2018_2_3.SuperTransform",
        "name": name,
        "id": nid,
        "baseType": "superNode",
        "nextNodes": [],
        "serialize": False,
        "description": None,
        "beforeActionAnnotations": annotations,
        "afterActionAnnotations": [],
        "actionNode": None,
    }


def iter_container_children(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk a `.v1.Container` clean step's internal nodes in execution order.

    A Clean step is serialized in one of two ways: as `.v1.Container` (the
    "Container" variant) whose operations live in `loomContainer.nodes` as
    single-action child nodes (`.v1.RenameColumn`, `.v1.AddColumn`, ...) chained
    linearly via nextNodes, or as a flat `.v2018_2_3.SuperTransform` storing the
    same operation objects in `beforeActionAnnotations[].annotationNode`. This
    function handles the Container variant.

    Returns children in chain order (BFS from loomContainer.initialNodes);
    unreachable children are appended last so nothing is silently dropped.
    Returns [] when the node has no loomContainer.
    """
    lc = node.get("loomContainer") or {}
    children = lc.get("nodes") or {}
    order: list[str] = []
    queue = list(lc.get("initialNodes") or [])
    while queue:
        cur = queue.pop(0)
        if cur in order or cur not in children:
            continue
        order.append(cur)
        for nxt in children[cur].get("nextNodes") or []:
            nid = nxt.get("nextNodeId") if isinstance(nxt, dict) else nxt
            if nid and nid not in order and nid not in queue:
                queue.append(nid)
    for cid in children:
        if cid not in order:
            order.append(cid)
    return [children[cid] for cid in order]


def container_convertibility(node: dict[str, Any]) -> list[str]:
    """Return [] when a `.v1.Container` is losslessly convertible to a flat
    SuperTransform, else the list of blocking reasons.

    Convertible = the container is a plain linear Clean step: one
    Default-namespace input, one Default-namespace output, and a single linear
    chain of transform children. Anything else (multi namespace, branching
    chain, nested container) must be transcribed verbatim instead - see
    references/tfl-json-schema.md.
    """
    problems: list[str] = []
    lc = node.get("loomContainer") or {}
    children = lc.get("nodes") or {}
    ns_in = node.get("namespacesToInput") or {}
    ns_out = node.get("namespacesToOutput") or {}
    if not children and not ns_in and not ns_out:
        # Empty Clean step (Container equivalent of an actions=0 SuperTransform):
        # converts to an empty SuperTransform / gets dropped at decompose.
        return []
    if len(ns_in) != 1:
        problems.append(f"namespacesToInput has {len(ns_in)} entries (expected 1)")
    if len(ns_out) != 1:
        problems.append(f"namespacesToOutput has {len(ns_out)} entries (expected 1)")
    initial = lc.get("initialNodes") or []
    if len(initial) != 1:
        problems.append(f"loomContainer.initialNodes has {len(initial)} entries (expected 1)")
    if ns_in and initial:
        entry = next(iter(ns_in.values())).get("nodeId")
        if entry != initial[0]:
            problems.append("namespacesToInput entry != loomContainer.initialNodes[0]")
    exits = [cid for cid, c in children.items() if not (c.get("nextNodes") or [])]
    if len(exits) != 1:
        problems.append(f"{len(exits)} terminal children (expected 1 linear chain)")
    elif ns_out:
        out_id = next(iter(ns_out.values())).get("nodeId")
        if out_id != exits[0]:
            problems.append("namespacesToOutput entry != terminal child")
    for cid, c in children.items():
        if len(c.get("nextNodes") or []) > 1:
            problems.append(f"child {cid[:8]} branches ({len(c['nextNodes'])} nextNodes)")
        if c.get("nodeType", "").endswith(".Container"):
            problems.append(f"nested container at child {cid[:8]}")
    # iter_container_children appends unreachable children last; detect them
    # by re-walking reachability strictly from initialNodes.
    seen: set[str] = set()
    queue = list(initial)
    while queue:
        cur = queue.pop(0)
        if cur in seen or cur not in children:
            continue
        seen.add(cur)
        for nxt in children[cur].get("nextNodes") or []:
            nid = nxt.get("nextNodeId") if isinstance(nxt, dict) else nxt
            if nid:
                queue.append(nid)
    if len(seen) != len(children):
        problems.append(f"{len(children) - len(seen)} children unreachable from initialNodes")
    return problems


def container_to_supertransform(node: dict[str, Any]) -> dict[str, Any]:
    """Convert a `.v1.Container` Clean step into the flat
    `.v2018_2_3.SuperTransform` equivalent (the two are logically equivalent
    representations of the same Clean step).

    The shell keeps id / name / nextNodes / description so outer wiring is
    untouched; children are wrapped as {"namespace": "Default",
    "annotationNode": <child>} in chain order. Children already carry the
    intra-chain nextNodes (last one is []), matching how flat-format
    annotationNodes chain.

    Raises ValueError when container_convertibility() reports blockers -
    callers fall back to verbatim transcription of the Container node.
    """
    problems = container_convertibility(node)
    if problems:
        raise ValueError(
            f"Container '{node.get('name')}' is not convertible: " + "; ".join(problems)
        )
    annotations = [
        {"namespace": "Default", "annotationNode": copy.deepcopy(child)}
        for child in iter_container_children(node)
    ]
    return {
        "nodeType": ".v2018_2_3.SuperTransform",
        "name": node.get("name"),
        "id": node.get("id"),
        "baseType": "superNode",
        "nextNodes": copy.deepcopy(node.get("nextNodes") or []),
        "serialize": False,
        "description": node.get("description"),
        "beforeActionAnnotations": annotations,
        "afterActionAnnotations": [],
        "actionNode": None,
    }


def normalize_source_containers(
    source_flow: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Return a copy of source_flow with every convertible `.v1.Container`
    clean step rewritten as a flat `.v2018_2_3.SuperTransform`.

    Some flows serialize Clean steps as Containers (see iter_container_children);
    the rest of this toolchain (copy_source_node, split_supertransform_actions,
    the extractor inventory, verify checks) speaks the flat SuperTransform +
    beforeActionAnnotations dialect. Running this pass once at the top of a build
    normalizes the whole source to that dialect so no downstream code needs a
    Container branch.

    Node id / name / nextNodes are preserved, so any node-id constants the build
    script references (e.g. N_CLEAN1) keep resolving and all edge wiring is intact.

    Non-convertible Containers (multi-namespace, branching, or nested - see
    container_convertibility) are left verbatim and named in the returned list so
    the caller can surface a warning; they must be whole-copied to a single layer,
    never actions-split.
    """
    out = copy.deepcopy(source_flow)
    skipped: list[str] = []
    for nid, node in list((out.get("nodes") or {}).items()):
        if not node.get("nodeType", "").endswith(".Container"):
            continue
        problems = container_convertibility(node)
        if problems:
            skipped.append(f"{node.get('name', nid)}: {'; '.join(problems)}")
            continue
        out["nodes"][nid] = container_to_supertransform(node)
    return out, skipped


# nodeProperties keys for incremental refresh / output refresh mode. These live
# at flow["nodeProperties"][<node-id>] (NOT on the node dicts), keyed by the
# serializer's fully-qualified class names.
INCREMENTAL_CONFIG_KEY = "com.tableau.loom.doc.fileformat.v2020_2_1.IncrementalConfiguration"
OUTPUT_REFRESH_OPTIONS_KEY = "com.tableau.loom.doc.fileformat.v2020_2_1.OutputRefreshOptions"


def set_incremental_refresh(
    flow: dict[str, Any],
    *,
    input_node_id: str,
    control_field: str,
    output_node_id: str,
    output_field: str,
    is_incremental_default: bool = True,
) -> None:
    """Configure incremental refresh + append output on a built flow.

    Mirrors what Prep Builder writes for "incremental refresh with append":
      - nodeProperties[input_node_id].IncrementalConfiguration: enabled,
        RefreshByOutput semantics - on each incremental run, read only input
        rows whose `control_field` exceeds max(`output_field`) already present
        in the output identified by `output_node_id`.
      - nodeProperties[output_node_id].OutputRefreshOptions: append on BOTH
        full and incremental runs (accumulating output). A full run therefore
        appends the whole current snapshot - duplicate rows if fired against an
        already-populated output. Run this flow incrementally.

    `is_incremental_default=True` marks incremental as the flow's default run
    mode (source flows authored in Prep UI often carry false here and rely on
    the Cloud schedule's run-type setting instead; REST-triggered runs have no
    runMode parameter in TSC, so the default is what fires).

    Only append/append is supported because that is the only combination
    observed and verified; other output operations would be guesswork.

    Merges into existing nodeProperties entries (does not clobber other keys).
    """
    nodes = flow.get("nodes") or {}
    if input_node_id not in nodes:
        raise KeyError(f"input_node_id {input_node_id} not in flow nodes")
    if output_node_id not in nodes:
        raise KeyError(f"output_node_id {output_node_id} not in flow nodes")
    props = flow.setdefault("nodeProperties", {})
    props.setdefault(input_node_id, {})[INCREMENTAL_CONFIG_KEY] = {
        "nodePropertyType": ".v2020_2_1.RefreshByOutput",
        "incrementalEnabled": True,
        "controlFieldName": control_field,
        "outputNodeId": output_node_id,
        "outputFieldName": output_field,
    }
    props.setdefault(output_node_id, {})[OUTPUT_REFRESH_OPTIONS_KEY] = {
        "nodePropertyType": ".v2020_2_1.OutputRefreshOptions",
        "outputOperationType": "outputOperationTypeAppend",
        "incrementalOutputOperationType": "outputOperationTypeAppend",
        "isIncrementalDefault": is_incremental_default,
    }


def verify_lineage_closure(
    new_flow: dict[str, Any],
    source_flow: dict[str, Any],
    *,
    synthetic_input_lineage: dict[str, list[str]] | None = None,
) -> list[str]:
    """Verify each non-Input node in new_flow can trace back (via new_flow's
    reverse edges) to at least one Input whose source identity is a real
    ancestor of that node in source_flow's DAG.

    Catches the "wrong-branch attachment" misplacement: assigning a step to a
    .tfl whose declared Inputs do not actually feed that step in the source.

    A new flow's Input has a "source identity" of:
      (a) the same node id, if it was deepcopied from a source Input, OR
      (b) the set of source Inputs whose `datasourceName` matches this new
          Input's `datasourceName`, OR
      (c) one or more source node IDs supplied via `synthetic_input_lineage`
          (= mapping datasourceName -> list of source node IDs whose
          descendants are valid upstream for this new Input). Use this for
          cross-layer Inputs (LoadSqlProxy reading a PDS produced by another
          new .tfl); the descendant set should typically include the source
          Input(s) that originally fed the upstream new .tfl's included
          source nodes.

    Returns a list of human-readable issues (empty list = lineage closed).
    Newly generated split-copy nodes (with new UUIDs not in source) are
    skipped because their source identity cannot be unambiguously determined
    from JSON alone.
    """
    synthetic_input_lineage = synthetic_input_lineage or {}
    src_nodes = source_flow.get("nodes", {})
    new_nodes = new_flow.get("nodes", {})

    descendant_cache: dict[str, set[str]] = {}

    def descendants(nid: str) -> set[str]:
        if nid in descendant_cache:
            return descendant_cache[nid]
        # Use iterative DFS to avoid recursion limits on long chains
        result: set[str] = set()
        stack = [nid]
        while stack:
            cur = stack.pop()
            if cur in result or cur not in src_nodes:
                continue
            result.add(cur)
            for nx in src_nodes[cur].get("nextNodes", []) or []:
                child = nx.get("nextNodeId") if isinstance(nx, dict) else nx
                if child:
                    stack.append(child)
        descendant_cache[nid] = result
        return result

    new_parents: dict[str, list[str]] = {nid: [] for nid in new_nodes}
    for nid, n in new_nodes.items():
        for nx in n.get("nextNodes", []) or []:
            child = nx.get("nextNodeId") if isinstance(nx, dict) else nx
            if child and child in new_parents:
                new_parents[child].append(nid)

    new_input_source_ids: dict[str, set[str]] = {}
    for nid, n in new_nodes.items():
        if n.get("baseType") != "input":
            continue
        candidates: set[str] = set()
        if nid in src_nodes and src_nodes[nid].get("baseType") == "input":
            candidates.add(nid)
        ds_name = (n.get("connectionAttributes") or {}).get("datasourceName")
        if ds_name:
            for src_nid, src_n in src_nodes.items():
                if src_n.get("baseType") != "input":
                    continue
                src_ds = (src_n.get("connectionAttributes") or {}).get("datasourceName")
                if src_ds and src_ds == ds_name:
                    candidates.add(src_nid)
            for synth_id in synthetic_input_lineage.get(ds_name, []):
                candidates.add(synth_id)
        new_input_source_ids[nid] = candidates

    issues: list[str] = []
    for nid, n in new_nodes.items():
        if n.get("baseType") == "input":
            continue
        if nid not in src_nodes:
            continue  # split-copy with new UUID; not verifiable here

        reachable_inputs: set[str] = set()
        visited = {nid}
        queue = list(new_parents.get(nid, []))
        while queue:
            cur = queue.pop(0)
            if cur in visited:
                continue
            visited.add(cur)
            cur_node = new_nodes.get(cur, {})
            if cur_node.get("baseType") == "input":
                reachable_inputs.add(cur)
            else:
                queue.extend(new_parents.get(cur, []))

        ok = False
        for inp_id in reachable_inputs:
            for src_inp_id in new_input_source_ids.get(inp_id, set()):
                if nid in descendants(src_inp_id):
                    ok = True
                    break
            if ok:
                break

        if not ok:
            reachable_descr = []
            for inp_id in reachable_inputs:
                inp_node = new_nodes[inp_id]
                ds = (inp_node.get("connectionAttributes") or {}).get("datasourceName", "?")
                reachable_descr.append(f"{inp_id[:8]} ({inp_node.get('name', '?')!r}, datasource={ds!r})")
            issues.append(
                f"lineage break: node {nid[:8]} ({n.get('name', '?')!r}) — "
                f"reachable Inputs in new flow [{', '.join(reachable_descr) or '<none>'}] "
                f"do not include any source-DAG ancestor of {nid[:8]}. "
                f"Walk source.tfl's Prev chain from {nid[:8]} to find the correct upstream Input."
            )
    return issues


def patch_pds_dbname(
    flow: dict[str, Any],
    *,
    datasource_name: str,
    project_name: str | None,
    dbname: str,
) -> int:
    """Set `dbname` on every LoadSqlProxy / dataConnection that targets the PDS.

    Matches by `datasourceName` (and `projectName` if provided). Used after the
    upstream layer has been published/run, so the downstream .tfl can be patched
    with the actual Cloud-assigned physical Hyper name.

    Returns the number of (node, dataConnection) pairs updated.
    """
    updated = 0
    for node in (flow.get("nodes") or {}).values():
        if not node.get("nodeType", "").endswith("LoadSqlProxy"):
            continue
        attrs = node.get("connectionAttributes") or {}
        if attrs.get("datasourceName") != datasource_name:
            continue
        if project_name is not None and attrs.get("projectName") != project_name:
            continue
        attrs["dbname"] = dbname
        node["connectionAttributes"] = attrs
        updated += 1
    for dconn in (flow.get("dataConnections") or {}).values():
        modified = dconn.get("modifiedConnectionAttributes") or {}
        if modified.get("datasourceName") != datasource_name:
            continue
        if project_name is not None and modified.get("projectName") != project_name:
            continue
        modified["dbname"] = dbname
        dconn["modifiedConnectionAttributes"] = modified
    return updated


def add_pds_input(
    flow: dict[str, Any],
    *,
    server_url: str,
    site_url_name: str,
    project_name: str,
    datasource_name: str,
    dbname: str | None = None,
    fields: list[dict[str, Any]] | None = None,
    name: str | None = None,
    node_id: str | None = None,
    next_nodes: list[dict[str, str]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """One-call helper: register Server conn + PDS dataConn + build LoadSqlProxy node.

    Inserts the node into `flow['nodes']` and returns `(node_id, node_dict)`.
    Use this when adding a cross-layer Input that reads from an upstream PDS.
    """
    conn_id = register_server_connection(
        flow, server_url=server_url, site_url_name=site_url_name
    )
    dconn_id = register_pds_data_connection(
        flow,
        base_connection_id=conn_id,
        project_name=project_name,
        datasource_name=datasource_name,
        dbname=dbname,
    )
    node = make_load_sql_proxy_node(
        data_connection_id=dconn_id,
        project_name=project_name,
        datasource_name=datasource_name,
        dbname=dbname,
        fields=fields,
        name=name,
        node_id=node_id,
        next_nodes=next_nodes,
    )
    flow.setdefault("nodes", {})[node["id"]] = node
    return node["id"], node


def copy_source_node(
    source_flow: dict[str, Any],
    node_id: str,
    *,
    kept_children: set[str],
) -> dict[str, Any]:
    """Deep-copy a node from source_flow with edges pruned to kept_children.

    Returned node has its `nextNodes` filtered to only those entries whose
    `nextNodeId` is in `kept_children`. The `nextNamespace` (and `namespace`)
    fields on the retained edges are preserved VERBATIM — callers must never
    rewrite these themselves, because non-Default values encode Union input
    namespace mappings and Join Left/Right side identity. Dropping these to
    "Default" causes silent run-time failures like "Union step is missing a
    connection" or "missing field on left side in join clause".

    Use this in prep-builder Step 3a when extracting included nodes from the
    source .tfl into a new .tfl.
    """
    src = source_flow.get("nodes", {}).get(node_id)
    if src is None:
        raise KeyError(f"node {node_id} not found in source_flow")
    node = copy.deepcopy(src)
    node["nextNodes"] = [
        nx for nx in (node.get("nextNodes") or [])
        if nx.get("nextNodeId") in kept_children
    ]
    return node


def wire_new_input_to_child(
    new_flow: dict[str, Any],
    *,
    lsp_node_id: str,
    child_id: str,
    source_flow: dict[str, Any],
    replaced_source_parent_id: str,
) -> None:
    """Add a nextNodes edge on lsp_node_id -> child_id, inheriting namespace.

    Looks up the edge `(replaced_source_parent_id, child_id)` in source_flow
    and copies its `nextNamespace` onto the new edge. This is how a new
    LoadSqlProxy that stands in for a source-flow parent of a Union/Join
    preserves the input identity that the Union's namespaceFieldMappings or
    Join's left/right resolver expects.

    If no such source edge exists (e.g. child is a brand-new node), the new
    edge gets `nextNamespace: "Default"`.

    Mutates `new_flow["nodes"][lsp_node_id]["nextNodes"]` in place.
    """
    lsp = new_flow.get("nodes", {}).get(lsp_node_id)
    if lsp is None:
        raise KeyError(f"lsp_node_id {lsp_node_id} not in new_flow")

    ns = "Default"
    src_parent = source_flow.get("nodes", {}).get(replaced_source_parent_id)
    if src_parent is not None:
        for nx in src_parent.get("nextNodes") or []:
            if nx.get("nextNodeId") == child_id:
                ns = nx.get("nextNamespace") or "Default"
                break

    lsp.setdefault("nextNodes", []).append({
        "namespace": "Default",
        "nextNodeId": child_id,
        "nextNamespace": ns,
    })


def verify_edge_namespaces(
    new_flow: dict[str, Any],
    source_flow: dict[str, Any],
    *,
    parent_substitutions: dict[str, str] | None = None,
    bridged_edges: set[tuple[str, str, str]] | None = None,
) -> list[str]:
    """Verify every edge in new_flow has the correct `nextNamespace`.

    For each `(parent_id, child_id)` edge in new_flow:
      - If both endpoints exist in source_flow, the new edge's `nextNamespace`
        must match the source edge's.
      - If `parent_id` is in `parent_substitutions` (e.g. a new LoadSqlProxy
        replacing a source parent), the source edge `(parent_substitutions[parent_id],
        child_id)` is consulted instead.
      - Edges with no source counterpart (both endpoints brand-new) are skipped.
      - Edges listed in `bridged_edges` (as `(parent_id, child_id, ns)` in
        new-flow ids) are skipped: they bridge over an excluded intermediate
        node, so their namespace was inherited verbatim from a DIFFERENT
        source edge (the final hop into the child) and a direct
        parent->child comparison would be a false mismatch.

    Returns a list of human-readable issues (empty list = all edges OK).
    Run this after build (typically alongside `verify_lineage_closure`) to
    catch the namespace-flatten regression.
    """
    parent_substitutions = parent_substitutions or {}
    bridged_edges = bridged_edges or set()
    src_nodes = source_flow.get("nodes", {}) or {}
    issues: list[str] = []

    for pid, pn in (new_flow.get("nodes") or {}).items():
        src_pid = parent_substitutions.get(pid, pid)
        src_parent = src_nodes.get(src_pid)

        for nx in pn.get("nextNodes") or []:
            cid = nx.get("nextNodeId")
            if not cid:
                continue
            ns_new = nx.get("nextNamespace") or "Default"
            if (pid, cid, ns_new) in bridged_edges:
                continue

            # If the child does not exist in source, the edge is internal to
            # the new flow (e.g. -> Output) and not verifiable here.
            if cid not in src_nodes:
                continue
            # If the parent is not represented in source (no substitution either),
            # likewise unverifiable.
            if src_parent is None:
                continue

            src_ns = None
            for s_nx in src_parent.get("nextNodes") or []:
                if s_nx.get("nextNodeId") == cid:
                    src_ns = s_nx.get("nextNamespace") or "Default"
                    break
            if src_ns is None:
                # parent existed in source but did not feed cid there. Likely a
                # restructure; skip rather than flag.
                continue

            if ns_new != src_ns:
                p_label = f"{pid[:8]}"
                if src_pid != pid:
                    p_label += f" (-> source parent {src_pid[:8]})"
                issues.append(
                    f"edge namespace mismatch: {p_label} -> {cid[:8]}: "
                    f"new={ns_new!r} source={src_ns!r}"
                )

    return issues


def pack_flow_json(
    flow: dict[str, Any],
    tfl_path: str | Path,
    *,
    aux_entries: dict[str, bytes] | None = None,
) -> Path:
    """Write a Python flow dict as a .tfl (zip with `flow` + optional aux entries).

    `aux_entries` lets the caller copy `maestroMetadata`, `displaySettings`, etc.
    from the source .tfl verbatim. Without `maestroMetadata` the resulting .tfl
    will NOT publish to Tableau Server (errorCode=280003). See PUBLISHABLE_AUX_ENTRIES.

    Caller is responsible for keeping the schema consistent with the target
    Tableau Prep version. Creates parent directories as needed.
    """
    out = Path(tfl_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(flow, ensure_ascii=False)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("flow", payload)
        for name, data in (aux_entries or {}).items():
            z.writestr(name, data)
    return out
