"""Shared model for decomposition plan.json (schema: references/plan-json-schema.md).

Three consumers share this module so a plan means the same thing everywhere:

  - prep-architect  `gen_plan_skeleton.py` (emit mechanical skeleton) and
                    `render_plan_md.py`   (render Stop-2 review markdown +
                                           validate the plan before review)
  - prep-builder    `build_from_plan.py`  (materialize .tfl / augmenter specs)

Responsibilities:
  - load + structural validation of plan.json (`load_plan`)
  - step-index <-> node-UUID resolution against the source flow via the
    canonical `flow_io.bfs_order` numbering (`StepResolver`)
  - wiring-graph computation for each kind=tfl entry (`compute_flow_graph`):
    the single implementation of edge derivation (split remapping, excluded-
    node bridging, input substitution, sink/output attachment) that both the
    markdown renderer (Upstream lineage table) and the builder consume
  - effective schema of a pds_augment stg (`augment_output_fields`)
  - deploy-context.md parsing (`parse_deploy_context`) for skeleton generation

No Tableau REST calls here; everything is local file computation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flow_io import bfs_order

SCHEMA_VERSION = "1"
LAYERS = ("staging", "intermediate", "marts")
FLOW_KINDS = ("tfl", "pds_augment")
TRANSFORM_OPS = ("rename", "cast", "hide")
INPUT_KINDS = ("upstream_pds", "passthrough_pds", "transplant")


# ---------------------------------------------------------------------------
# Loading / structural validation
# ---------------------------------------------------------------------------

class PlanError(ValueError):
    """Structural problem in plan.json. Message lists every issue found."""


def _require(cond: bool, issues: list[str], msg: str) -> None:
    if not cond:
        issues.append(msg)


def load_plan(path: str | Path) -> dict[str, Any]:
    """Read plan.json and validate its structure (no source flow needed yet).

    Raises PlanError with ALL problems listed (not just the first) so the
    author can fix them in one pass.
    """
    plan = json.loads(Path(path).read_text(encoding="utf-8"))
    issues = validate_plan_structure(plan)
    if issues:
        raise PlanError(
            f"plan.json ({path}) has {len(issues)} issue(s):\n  - "
            + "\n  - ".join(issues)
        )
    return plan


def validate_plan_structure(plan: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    _require(plan.get("schema_version") == SCHEMA_VERSION, issues,
             f"schema_version must be '{SCHEMA_VERSION}'")
    _require(bool(plan.get("flow_name")), issues, "flow_name is required")

    server = plan.get("server") or {}
    _require(bool(server.get("url")), issues, "server.url is required")
    _require("site_url_name" in server, issues, "server.site_url_name is required")

    for group in ("ds_projects", "flow_projects"):
        projs = plan.get(group) or {}
        for layer in LAYERS:
            entry = projs.get(layer) or {}
            _require(bool(entry.get("path")) and bool(entry.get("luid")), issues,
                     f"{group}.{layer} needs both 'path' and 'luid'")

    original = plan.get("original") or {}
    _require("flow_luid" in original, issues,
             "original.flow_luid is required (null allowed)")
    outputs = original.get("outputs")
    _require(isinstance(outputs, list) and len(outputs) > 0, issues,
             "original.outputs must be a non-empty list of {name, luid}")
    original_output_names = {o.get("name") for o in outputs or [] if isinstance(o, dict)}

    flows = plan.get("flows")
    if not isinstance(flows, list) or not flows:
        issues.append("flows must be a non-empty list")
        return issues

    names_seen: set[str] = set()
    for i, f in enumerate(flows):
        tag = f"flows[{i}] ({f.get('name', '?')})"
        name = f.get("name")
        _require(bool(name), issues, f"{tag}: name is required")
        if name in names_seen:
            issues.append(f"{tag}: duplicate flow name")
        names_seen.add(name)
        _require(f.get("layer") in LAYERS, issues,
                 f"{tag}: layer must be one of {LAYERS}")
        kind = f.get("kind")
        _require(kind in FLOW_KINDS, issues,
                 f"{tag}: kind must be one of {FLOW_KINDS}")
        soon = f.get("source_original_output_name")
        if soon is not None and soon not in original_output_names:
            issues.append(
                f"{tag}: source_original_output_name {soon!r} not in "
                f"original.outputs names {sorted(original_output_names)}"
            )
        if kind == "pds_augment":
            _require(f.get("layer") == "staging", issues,
                     f"{tag}: kind=pds_augment is staging-only")
            _require(isinstance(f.get("source_input_step"), int), issues,
                     f"{tag}: source_input_step (int) is required")
            issues.extend(_validate_transforms(f.get("transforms"), tag))
        else:
            issues.extend(_validate_tfl_entry(f, tag))
    return issues


def _validate_transforms(transforms: Any, tag: str) -> list[str]:
    issues: list[str] = []
    if not isinstance(transforms, list):
        return [f"{tag}: transforms must be a list"]
    for j, t in enumerate(transforms):
        ttag = f"{tag}.transforms[{j}]"
        op = t.get("op")
        if op not in TRANSFORM_OPS:
            issues.append(f"{ttag}: op must be one of {TRANSFORM_OPS}")
            continue
        if not t.get("column_name"):
            issues.append(f"{ttag}: column_name is required")
        if op in ("rename", "cast") and not t.get("to_caption"):
            issues.append(f"{ttag}: to_caption is required for op={op}")
        if op == "cast" and not t.get("to_datatype"):
            issues.append(f"{ttag}: to_datatype is required for op=cast")
    return issues


def _validate_tfl_entry(f: dict[str, Any], tag: str) -> list[str]:
    issues: list[str] = []
    included = f.get("included_steps", [])
    if not isinstance(included, list) or not all(isinstance(s, int) for s in included):
        issues.append(f"{tag}: included_steps must be a list of ints")
        included = []
    split_steps: set[int] = set()
    for j, sp in enumerate(f.get("splits", []) or []):
        stag = f"{tag}.splits[{j}]"
        step = sp.get("step")
        if not isinstance(step, int):
            issues.append(f"{stag}: step (int) is required")
            continue
        if step in included:
            issues.append(f"{stag}: step {step} is also in included_steps "
                          "(a split node REPLACES the source step in this flow)")
        if step in split_steps:
            issues.append(f"{stag}: duplicate split for step {step} in one flow")
        split_steps.add(step)
        idx = sp.get("action_indices")
        if not isinstance(idx, list) or not idx or not all(isinstance(k, int) for k in idx):
            issues.append(f"{stag}: action_indices must be a non-empty list of ints")
        if not sp.get("new_name"):
            issues.append(f"{stag}: new_name is required")
    inputs = f.get("inputs", []) or []
    if f.get("input_status") != "needs_provisioning" and not inputs:
        issues.append(f"{tag}: inputs must be non-empty (or set "
                      "input_status=needs_provisioning)")
    # Thin republish (no source steps carried over): inputs have nothing to
    # re-wire, so replaces_steps is meaningless and may be omitted.
    has_steps = bool(included) or bool(f.get("splits"))
    for j, inp in enumerate(inputs):
        itag = f"{tag}.inputs[{j}]"
        ikind = inp.get("kind")
        if ikind not in INPUT_KINDS:
            issues.append(f"{itag}: kind must be one of {INPUT_KINDS}")
            continue
        if ikind == "transplant":
            if not isinstance(inp.get("step"), int):
                issues.append(f"{itag}: step (int) is required for transplant")
            continue
        if not inp.get("pds_name"):
            issues.append(f"{itag}: pds_name is required")
        rs = inp.get("replaces_steps")
        if has_steps:
            if not isinstance(rs, list) or not rs or not all(isinstance(s, int) for s in rs):
                issues.append(f"{itag}: replaces_steps must be a non-empty list of ints "
                              "(the IMMEDIATE source parents this input stands in for)")
        elif rs is not None and not (isinstance(rs, list) and all(isinstance(s, int) for s in rs)):
            issues.append(f"{itag}: replaces_steps must be a list of ints when present")
        if ikind == "passthrough_pds":
            if not inp.get("project_path"):
                issues.append(f"{itag}: project_path is required for passthrough_pds")
            if not inp.get("luid"):
                issues.append(f"{itag}: luid is required for passthrough_pds")
    out = f.get("output") or {}
    if f.get("input_status") != "needs_provisioning" and not out.get("name"):
        issues.append(f"{tag}: output.name is required")
    for j, rb in enumerate(f.get("rename_back", []) or []):
        if not rb.get("from") or not rb.get("to"):
            issues.append(f"{tag}.rename_back[{j}]: needs 'from' and 'to'")
    inc = f.get("incremental")
    if inc is not None:
        for key in ("input", "control_field", "output_field"):
            if not inc.get(key):
                issues.append(f"{tag}.incremental: '{key}' is required")
    return issues


# ---------------------------------------------------------------------------
# Step resolution against the source flow
# ---------------------------------------------------------------------------

class StepResolver:
    """Maps plan step indexes (1-based, flow-summary Topology numbering) to
    source node UUIDs and back, using the canonical bfs_order."""

    def __init__(self, source_flow: dict[str, Any]):
        self.flow = source_flow
        self.order = bfs_order(source_flow)
        self.uuid_by_step = {i + 1: nid for i, nid in enumerate(self.order)}
        self.step_by_uuid = {nid: i + 1 for i, nid in enumerate(self.order)}

    def uuid(self, step: int) -> str:
        try:
            return self.uuid_by_step[step]
        except KeyError:
            raise PlanError(
                f"step {step} out of range (source flow has {len(self.order)} nodes)"
            ) from None

    def node(self, step: int) -> dict[str, Any]:
        return self.flow["nodes"][self.uuid(step)]

    def label(self, step: int) -> str:
        n = self.node(step)
        return f"#{step} {n.get('name', '?')}"


# ---------------------------------------------------------------------------
# Wiring graph
# ---------------------------------------------------------------------------

@dataclass
class GraphNode:
    key: str                      # unique within the graph
    role: str                     # "included" | "split" | "transplant" | "lsp" | "output" | "rename_back"
    step: int | None = None       # source step (included / split / transplant)
    source_uuid: str | None = None
    input_index: int | None = None  # for role=lsp / transplant: index into entry["inputs"]
    label: str = ""


@dataclass
class GraphEdge:
    src: str
    dst: str
    ns: str                       # nextNamespace to write on the edge
    bridged_over: list[int] = field(default_factory=list)  # steps skipped by bridging


@dataclass
class FlowGraph:
    nodes: dict[str, GraphNode]
    edges: list[GraphEdge]
    sink_key: str                 # node the Output (or rename-back) attaches to
    issues: list[str]             # hard errors — do not build if non-empty
    notes: list[str]              # informational (bridges applied, etc.)
    # per included/split node: which input keys reach it (for lineage table)
    reachable_inputs: dict[str, set[str]] = field(default_factory=dict)


def _next_edges(node: dict[str, Any]) -> list[tuple[str, str]]:
    """[(child_id, nextNamespace), ...] from a source node."""
    out = []
    for nx in node.get("nextNodes", []) or []:
        cid = nx.get("nextNodeId") if isinstance(nx, dict) else nx
        if cid:
            ns = nx.get("nextNamespace") or "Default" if isinstance(nx, dict) else "Default"
            out.append((cid, ns))
    return out


def compute_flow_graph(
    entry: dict[str, Any],
    resolver: StepResolver,
    plan: dict[str, Any],
) -> FlowGraph:
    """Derive the complete wiring of one kind=tfl plan entry.

    Rules (shared by renderer and builder — change here, not in either script):
      - included steps keep their source edges to other represented nodes
      - a split node REPLACES its source step inside this flow (same wiring)
      - edges pointing at an excluded node are bridged forward through it,
        but only while the excluded node is single-parent AND single-child;
        anything else is a hard issue (fix the plan, don't guess)
      - each lsp/transplant input feeds the represented children of the
        source steps it replaces, inheriting each source edge's nextNamespace
      - the single sink gets the Output (via rename-back when declared);
        multiple sinks require attach_output_to_step
    """
    src_nodes = resolver.flow["nodes"]
    issues: list[str] = []
    notes: list[str] = []
    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []

    # --- represent: source uuid -> graph key
    rep: dict[str, str] = {}
    for step in entry.get("included_steps", []) or []:
        u = resolver.uuid(step)
        key = u
        nodes[key] = GraphNode(key=key, role="included", step=step, source_uuid=u,
                               label=resolver.label(step))
        rep[u] = key
    for sp in entry.get("splits", []) or []:
        step = sp["step"]
        u = resolver.uuid(step)
        key = f"split:{step}"
        nodes[key] = GraphNode(key=key, role="split", step=step, source_uuid=u,
                               label=f"{resolver.label(step)} [split]")
        rep[u] = key

    # --- inputs
    prev_map: dict[str, list[str]] = {nid: [] for nid in src_nodes}
    for nid, n in src_nodes.items():
        for cid, _ns in _next_edges(n):
            if cid in prev_map:
                prev_map[cid].append(nid)

    def bridge(src_label: str, first_child: str, ns_in: str
               ) -> tuple[str, str, list[int]] | None:
        """Resolve a source edge that points at an excluded node.

        Forward-searches the excluded territory for represented nodes:
          - exactly one distinct (target, namespace) -> bridge to it, skipping
            the excluded steps in between (e.g. an empty Clean dropped by the
            plan); the new edge carries the namespace of the FINAL source hop
            (the edge into the represented node), which is what Union/Join
            input identity needs
          - none -> the subtree belongs to other flows; the edge is dropped
            (normal at fan-out boundaries)
          - several -> ambiguous, hard issue (include the branching step)
        Bridging through a merge point (an excluded step with >1 parents on
        the used path) is a hard issue — it would silently lose the other
        branch's rows.
        """
        hits: list[tuple[str, str, list[int]]] = []
        seen: set[str] = set()
        stack: list[tuple[str, str, list[int]]] = [(first_child, ns_in, [])]
        while stack:
            cur, ns, path = stack.pop()
            if cur in rep:
                hits.append((rep[cur], ns, path))
                continue
            if cur in seen:
                continue
            seen.add(cur)
            cur_node = src_nodes.get(cur)
            if cur_node is None or cur_node.get("baseType") == "output":
                continue  # original Output / dead end — not carried over
            step = resolver.step_by_uuid[cur]
            for cid2, ns2 in _next_edges(cur_node):
                stack.append((cid2, ns2, path + [step]))
        if not hits:
            notes.append(f"{src_label}: edge into excluded subtree dropped "
                         "(covered by other flows / original Output)")
            return None
        distinct = {(h[0], h[1]) for h in hits}
        if len(distinct) > 1:
            issues.append(
                f"{src_label}: edge into excluded step(s) reaches multiple "
                f"represented targets {sorted(nodes[k].label for k, _ in distinct)} "
                "— include the branching step in this flow or re-plan"
            )
            return None
        dst_key, ns_final, path = hits[0]
        for step in path:
            if len(prev_map.get(resolver.uuid(step), [])) > 1:
                issues.append(
                    f"{src_label}: cannot bridge through excluded step "
                    f"{resolver.label(step)} — it is a merge point "
                    f"({len(prev_map[resolver.uuid(step)])} parents); include it"
                )
                return None
        return dst_key, ns_final, path

    # --- edges among represented nodes (with bridging)
    for key, gn in list(nodes.items()):
        for cid, ns in _next_edges(src_nodes[gn.source_uuid]):
            hop = bridge(gn.label, cid, ns)
            if hop is None:
                continue
            dst_key, ns_final, skipped = hop
            if skipped:
                notes.append(
                    f"bridged {gn.label} → {nodes[dst_key].label} over excluded "
                    f"step(s) {skipped}"
                )
            edges.append(GraphEdge(src=key, dst=dst_key, ns=ns_final,
                                   bridged_over=skipped))

    # --- input nodes
    plan_flow_by_name = {f["name"]: f for f in plan.get("flows", [])}
    for i, inp in enumerate(entry.get("inputs", []) or []):
        ikind = inp["kind"]
        if ikind == "transplant":
            step = inp["step"]
            u = resolver.uuid(step)
            if src_nodes[u].get("baseType") != "input":
                issues.append(f"inputs[{i}]: transplant step {step} is not an "
                              "input node in the source flow")
                continue
            key = f"input#{i}"
            nodes[key] = GraphNode(key=key, role="transplant", step=step,
                                   source_uuid=u, input_index=i,
                                   label=f"{resolver.label(step)} [transplant]")
            wired = False
            for cid, ns in _next_edges(src_nodes[u]):
                hop = bridge(nodes[key].label, cid, ns)
                if hop is None:
                    continue
                dst_key, ns_final, skipped = hop
                edges.append(GraphEdge(src=key, dst=dst_key, ns=ns_final,
                                       bridged_over=skipped))
                wired = True
            if not wired:
                issues.append(f"inputs[{i}]: transplant step {step} feeds no "
                              "represented node in this flow")
            continue

        # upstream_pds / passthrough_pds
        pds_name = inp["pds_name"]
        if ikind == "upstream_pds" and pds_name not in plan_flow_by_name:
            issues.append(
                f"inputs[{i}]: upstream_pds {pds_name!r} does not match any plan "
                "flow name — use passthrough_pds for pre-existing PDSes"
            )
        key = f"input#{i}"
        nodes[key] = GraphNode(key=key, role="lsp", input_index=i,
                               label=f"{pds_name} [PDS]")
        replaces = inp.get("replaces_steps") or []
        if not replaces and not rep:
            continue  # thin republish: the input IS the flow body
        wired = False
        for r_step in replaces:
            r_uuid = resolver.uuid(r_step)
            for cid, ns in _next_edges(src_nodes[r_uuid]):
                hop = bridge(nodes[key].label, cid, ns)
                if hop is None:
                    continue
                dst_key, ns_final, skipped = hop
                edges.append(GraphEdge(src=key, dst=dst_key, ns=ns_final,
                                       bridged_over=skipped))
                wired = True
        if not wired:
            issues.append(
                f"inputs[{i}]: replaces_steps {replaces} feed no "
                "represented node — replaces_steps must be the IMMEDIATE source "
                "parents of steps in this flow"
            )

    # --- reachability from inputs (lineage table + closure pre-check)
    adj: dict[str, list[str]] = {k: [] for k in nodes}
    for e in edges:
        adj[e.src].append(e.dst)
    reachable_inputs: dict[str, set[str]] = {k: set() for k in nodes}
    for key, gn in nodes.items():
        if gn.role not in ("lsp", "transplant"):
            continue
        stack = [key]
        seen = set()
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            reachable_inputs[cur].add(key)
            stack.extend(adj[cur])
    for key, gn in nodes.items():
        if gn.role in ("included", "split") and not reachable_inputs[key]:
            issues.append(
                f"{gn.label}: not reachable from any declared input — "
                "wrong-branch placement or missing input (lineage closure)"
            )

    # --- sink / output attachment
    out_deg = {k: 0 for k in nodes}
    for e in edges:
        out_deg[e.src] += 1
    sinks = [k for k, d in out_deg.items() if d == 0]
    attach_step = entry.get("attach_output_to_step")
    sink_key = ""
    if attach_step is not None:
        u = resolver.uuid(attach_step)
        sink_key = rep.get(u, "")
        if not sink_key:
            issues.append(f"attach_output_to_step {attach_step} is not a "
                          "represented node in this flow")
    elif len(sinks) == 1:
        sink_key = sinks[0]
    else:
        issues.append(
            f"flow has {len(sinks)} sink(s) "
            f"({[nodes[s].label for s in sinks]}); set attach_output_to_step"
        )

    return FlowGraph(nodes=nodes, edges=edges, sink_key=sink_key,
                     issues=issues, notes=notes,
                     reachable_inputs=reachable_inputs)


def validate_plan_with_source(
    plan: dict[str, Any], source_flow: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Full plan validation against the source flow.

    Returns (issues, notes). Empty issues == plan is buildable. Used by
    render_plan_md.py (pre-Stop-2 gate) and build_from_plan.py (build gate).
    """
    resolver = StepResolver(source_flow)
    issues: list[str] = []
    notes: list[str] = []

    declared_total = (plan.get("source") or {}).get("total_nodes")
    if declared_total is not None and declared_total != len(resolver.order):
        issues.append(
            f"source.total_nodes={declared_total} but source flow has "
            f"{len(resolver.order)} nodes — plan may target a different flow"
        )

    assigned: dict[int, list[str]] = {}
    for f in plan.get("flows", []):
        name = f.get("name", "?")
        try:
            if f.get("kind") == "pds_augment":
                step = f["source_input_step"]
                node = resolver.node(step)
                if node.get("baseType") != "input":
                    issues.append(f"{name}: source_input_step {step} is not an input node")
                assigned.setdefault(step, []).append(name)
                continue
            if f.get("input_status") == "needs_provisioning":
                continue
            for step in f.get("included_steps", []) or []:
                resolver.uuid(step)
                assigned.setdefault(step, []).append(name)
            for sp in f.get("splits", []) or []:
                node = resolver.node(sp["step"])
                n_actions = len(node.get("beforeActionAnnotations") or [])
                bad = [k for k in sp["action_indices"] if k < 0 or k >= n_actions]
                if bad:
                    issues.append(
                        f"{name}: split of {resolver.label(sp['step'])} references "
                        f"action_indices {bad} but the node has {n_actions} actions"
                    )
                assigned.setdefault(sp["step"], []).append(name)
            for inp in f.get("inputs", []) or []:
                if inp.get("kind") == "transplant":
                    resolver.uuid(inp["step"])
                else:
                    for s in inp.get("replaces_steps", []) or []:
                        resolver.uuid(s)
            graph = compute_flow_graph(f, resolver, plan)
            issues.extend(f"{name}: {i}" for i in graph.issues)
            notes.extend(f"{name}: {n}" for n in graph.notes)
            tok_issues, tok_notes = check_expression_tokens(f, resolver, plan)
            issues.extend(f"{name}: {i}" for i in tok_issues)
            notes.extend(f"{name}: {n}" for n in tok_notes)
        except PlanError as exc:
            issues.append(f"{name}: {exc}")

    dupes = {s: fl for s, fl in assigned.items() if len(fl) > 1}
    for step, fl in sorted(dupes.items()):
        # Same step in multiple flows is usually a mistake; splits of one node
        # across two flows are the legitimate case and land here too, so this
        # stays a note, not an issue.
        notes.append(f"step {resolver.label(step)} appears in {len(fl)} flows: {fl}")
    unassigned = [
        s for s in resolver.uuid_by_step
        if s not in assigned
        and resolver.node(s).get("baseType") not in ("input", "output")
    ]
    if unassigned:
        notes.append(
            "steps not assigned to any flow (dropped): "
            + ", ".join(resolver.label(s) for s in unassigned)
        )
    return issues, notes


# ---------------------------------------------------------------------------
# Expression-token check (naming-regime guard)
# ---------------------------------------------------------------------------

# Bracketed field refs inside Prep expression strings, e.g.
#   "{ PARTITION [銘柄]: { ORDERBY [約定日] ASC: ROW_NUMBER() } }"
_FIELD_TOKEN_RE = re.compile(r"\[([^\[\]]+)\]")
# Keys whose string values are Prep expressions (field refs live here).
_EXPRESSION_KEYS = ("expression", "filterExpression")
# Keys whose string values NAME a column — anything appearing here is a name
# the flow itself introduces or manipulates, hence legal for later refs.
_NAME_KEYS = ("columnName", "rename", "name", "caption", "outputFieldName",
              "fieldName", "column", "columnNames")
# Columns Tableau injects implicitly (SuperUnion adds `Table Names`).
_BUILTIN_COLUMNS = {"Table Names", "Number of Rows"}


def _collect_strings(obj: Any, keys: tuple[str, ...], out: set[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys:
                if isinstance(v, str):
                    out.add(v)
                elif isinstance(v, list):
                    out.update(x for x in v if isinstance(x, str))
            _collect_strings(v, keys, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_strings(v, keys, out)


def _input_exposed_names(
    inp: dict[str, Any],
    resolver: StepResolver,
    plan_flow_by_name: dict[str, dict[str, Any]],
) -> set[str] | None:
    """Column names one plan input exposes to the new flow, or None when the
    schema is not statically knowable (upstream kind=tfl flow — its output
    schema exists only after a run)."""
    kind = inp.get("kind")
    if kind == "transplant":
        node = resolver.node(inp["step"])
        names: set[str] = set()
        for f in node.get("fields") or []:
            for key in ("caption", "name"):
                if f.get(key):
                    names.add(f[key])
        # Input-node actions (Input renames) realize display names.
        _collect_strings(node.get("actions"), _NAME_KEYS, names)
        return names
    if kind == "passthrough_pds":
        names = set()
        for s in inp.get("replaces_steps") or []:
            node = resolver.node(s)
            if node.get("baseType") != "input":
                continue
            for f in node.get("fields") or []:
                for key in ("caption", "name"):
                    if f.get(key):
                        names.add(f[key])
            _collect_strings(node.get("actions"), _NAME_KEYS, names)
        return names or None
    # upstream_pds: schema known only when the upstream is a pds_augment
    # (transform-applied source fields); a tfl upstream is post-run only.
    up = plan_flow_by_name.get(inp.get("pds_name", ""))
    if up is not None and up.get("kind") == "pds_augment":
        return {f["caption"] for f in augment_output_fields(up, resolver)}
    return None


def check_expression_tokens(
    entry: dict[str, Any],
    resolver: StepResolver,
    plan: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Cross-check transcribed expressions against upstream exposed names.

    Catches the naming-regime break (e.g. a semantically-translated stg whose
    downstream verbatim-transcribed expressions still reference the original
    names) BEFORE Stop 2 / build, instead of at flow run time. Set-based on
    purpose: names created anywhere inside this flow's own nodes are allowed
    without ordering analysis — the target failure mode is a wholesale
    upstream-name mismatch, not a subtle ordering bug.

    Returns (issues, notes). When any input's schema is not statically
    knowable, the check is skipped with a note (never guess).
    """
    issues: list[str] = []
    notes: list[str] = []
    plan_flow_by_name = {f["name"]: f for f in plan.get("flows", [])}

    allowed: set[str] = set(_BUILTIN_COLUMNS)
    for inp in entry.get("inputs", []) or []:
        exposed = _input_exposed_names(inp, resolver, plan_flow_by_name)
        if exposed is None:
            notes.append(
                "式トークン照合 skip: 上流スキーマが静的に不明 "
                f"(input {inp.get('pds_name') or inp.get('step')})"
            )
            return issues, notes
        allowed |= exposed

    # Names introduced/manipulated by this flow's own transcribed nodes.
    tokens: set[str] = set()
    own_nodes: list[dict[str, Any]] = []
    for step in entry.get("included_steps", []) or []:
        own_nodes.append(resolver.node(step))
    for sp in entry.get("splits", []) or []:
        node = resolver.node(sp["step"])
        acts = node.get("beforeActionAnnotations") or []
        own_nodes.append({
            "beforeActionAnnotations": [acts[k] for k in sp["action_indices"]
                                        if 0 <= k < len(acts)]
        })
    for node in own_nodes:
        _collect_strings(node, _NAME_KEYS, allowed)
        exprs: set[str] = set()
        _collect_strings(node, _EXPRESSION_KEYS, exprs)
        for e in exprs:
            tokens.update(_FIELD_TOKEN_RE.findall(e))

    unknown = sorted(tokens - allowed)
    if unknown:
        shown = ", ".join(f"[{t}]" for t in unknown[:8])
        more = f" (+{len(unknown) - 8})" if len(unknown) > 8 else ""
        issues.append(
            f"転写式が上流スキーマに無い列を参照: {shown}{more} — "
            "上流 stg の to_caption が元名からズレている (semantic translation "
            "は禁止、input-policy.md §命名レジーム) か、included_steps / "
            "replaces_steps の配置ミス"
        )
    return issues, notes


# ---------------------------------------------------------------------------
# Augment schema
# ---------------------------------------------------------------------------

def augment_output_fields(
    entry: dict[str, Any],
    resolver: StepResolver,
) -> list[dict[str, Any]]:
    """Effective column schema of a pds_augment stg PDS: the source input's
    fields with the entry's transforms applied (renames change caption, casts
    change datatype, hides drop the column). Used as the `fields` list of
    downstream LoadSqlProxy inputs so Prep binds columns without a first run.
    """
    node = resolver.node(entry["source_input_step"])
    by_col: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for f in node.get("fields") or []:
        if f.get("isGenerated"):
            continue
        raw = f.get("name")
        if not raw:
            continue
        key = f"[{raw}]"
        by_col[key] = {"caption": f.get("caption") or raw,
                       "datatype": f.get("type") or "string"}
        order.append(key)
    for t in entry.get("transforms", []) or []:
        col = by_col.get(t.get("column_name"))
        if col is None:
            continue
        if t["op"] == "hide":
            col["hidden"] = True
        elif t["op"] == "rename":
            col["caption"] = t["to_caption"]
        elif t["op"] == "cast":
            col["caption"] = t.get("to_caption") or col["caption"]
            col["datatype"] = t.get("to_datatype") or col["datatype"]
    return [
        {"name": by_col[k]["caption"], "type": by_col[k]["datatype"],
         "caption": by_col[k]["caption"], "isGenerated": False}
        for k in order if not by_col[k].get("hidden")
    ]


# ---------------------------------------------------------------------------
# deploy-context.md parsing (for skeleton generation)
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_LAYER_ROW_RE = re.compile(
    r"^\|\s*`(flows|datasources)`\s*\|\s*`(\w+)`\s*\|\s*(\w+)\s*\|\s*`?([0-9a-f-]*)`?\s*\|",
    re.MULTILINE,
)
# deploy-context layer names -> plan.json layer names
_LAYER_ALIAS = {"stg": "staging", "intermediate": "intermediate", "marts": "marts"}


def parse_deploy_context(path: str | Path) -> dict[str, Any]:
    """Extract server / site / target / per-layer project LUIDs from
    deploy-context.md (generated mechanically by get_project_structure.py,
    so line-format parsing is stable).

    Returns {server, site_url_name?, target_path, target_luid,
             flow_projects: {layer: {path, luid}}, ds_projects: {...}}.
    Layers missing from the table (preflight pending) are omitted — the
    skeleton generator surfaces that as a to-fill marker.
    """
    text = Path(path).read_text(encoding="utf-8")
    fm_match = _FRONTMATTER_RE.match(text)
    fm: dict[str, str] = {}
    if fm_match:
        for line in fm_match.group(1).splitlines():
            if ":" in line and not line.startswith((" ", "\t", "-")):
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip()
    target_path = fm.get("target_path", "")
    out: dict[str, Any] = {
        "server": fm.get("server", ""),
        # frontmatter `site` is the site LUID; the site content-url lives in
        # .env SITE_NAME. Callers fill site_url_name from .env when needed.
        "site_luid": fm.get("site", ""),
        "target_path": target_path,
        "target_luid": fm.get("target_luid", ""),
        "flow_projects": {},
        "ds_projects": {},
    }
    parent_key = {"flows": "flow_projects", "datasources": "ds_projects"}
    for m in _LAYER_ROW_RE.finditer(text):
        parent, layer_raw, present, luid = m.groups()
        layer = _LAYER_ALIAS.get(layer_raw)
        if layer is None or present.lower() != "yes" or not luid:
            continue
        out[parent_key[parent]][layer] = {
            "path": f"{target_path}/{parent}/{layer_raw}",
            "luid": luid,
        }
    return out
