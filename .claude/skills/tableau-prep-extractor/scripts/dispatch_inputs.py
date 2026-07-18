"""Phase B helper: classify each Input node of a Prep flow and emit mechanical
findings as JSON.

Invoked by Phase B (cloud context extraction) after get_project_structure.py
has produced deploy-context.md. The script is run twice when re-scan is needed:
first to discover the parent projects of PDS Inputs that fall outside
target_path (so the caller can re-invoke get_project_structure.py with
--also-scan), then again against the updated deploy-context.md to finalize
LUID resolution. Output JSON is consumed by tableau-prep-architect, which folds the
findings into its decomposition plan and surfaces a single unified user
confirmation (Stop 2) covering both .tfl decomposition and Input policy /
rename proposals.

Mechanical responsibilities (handled here, NOT by any LLM):
- Classify each Input via flow_io.inspect_input_node (pds / vconn / direct_db /
  extract). `unknown` is treated as a hard error: if encountered the script
  exits 2 because it indicates the Skill premise is broken (Prep version drift
  or malformed flow). Running architect on top of it would produce a
  half-defined plan.
- For PDS Inputs: try to resolve PDS LUID by scanning deploy-context.md for a
  (projectName, datasourceName) match. Emit `resolved` when 1 unique match,
  `ambiguous` with the list when 2+, `unresolved` when 0.
- For vconn Inputs: collect resourceId (vconn LUID) directly from the base
  connection plus the bracket-parsed table_uuid/table_name.
- For direct_db Inputs: record the underlying connection class so architect
  can write a precise Cloud-side provisioning ask.
- For ANY Input: collect the field list (name + caption + datatype) with
  isGenerated=True entries filtered out.

Architect's responsibilities (downstream of this script):
- Semantic translation of non-ASCII captions (e.g. 数量 -> quantity)
- Compose the per-Input policy (passthrough / augment / needs_provisioning)
- Surface a single Stop 2 review covering both decomposition and Input policy

Usage:
    python dispatch_inputs.py \
        --flow-json work/<session>/flow.json \
        --deploy-context work/<session>/reports/deploy-context.md \
        --output work/<session>/reports/input-dispatch-mech.json

Exit codes:
    0   Success
    1   Argument / IO error
    2   At least one Input classified as `unknown` (Skill premise broken)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from flow_io import inspect_input_node, vconn_input_to_augmenter_columns  # noqa: E402


def parse_deploy_context_datasources(deploy_ctx_path: Path) -> list[dict[str, str]]:
    """Extract published datasources from deploy-context.md.

    Looks for a 'Datasources' or similar section containing markdown tables of
    (project_path, name, luid). Returns list of {project_path, name, luid}.
    deploy-context.md format is project_hierarchy.md governed; we expect rows
    like '| <project_path> | <ds_name> | <luid> |'.

    Empty list if no parseable datasource section is found (typical when only
    target_path was scanned without --also-scan). The caller treats every PDS
    as 'pds_luid_unresolved' in that case.
    """
    if not deploy_ctx_path.exists():
        return []
    text = deploy_ctx_path.read_text(encoding="utf-8")

    # Look for a section heading mentioning datasource(s) and read the next
    # markdown table after it. Be liberal about heading levels and casing.
    section_re = re.compile(
        r"^#{2,6}\s+.*?datasource.*?$",
        re.IGNORECASE | re.MULTILINE,
    )
    rows: list[dict[str, str]] = []
    for m in section_re.finditer(text):
        # Scan from the heading until the next heading or EOF for table rows.
        tail = text[m.end():]
        end_m = re.search(r"^#{2,6}\s+", tail, re.MULTILINE)
        block = tail[: end_m.start()] if end_m else tail
        # Each table row looks like '| col1 | col2 | col3 |'. We tolerate
        # variable column counts and pick rows that have at least 3 cells.
        for line in block.splitlines():
            line = line.strip()
            if not line.startswith("|") or not line.endswith("|"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            cells = [c for c in cells if c != ""]
            if len(cells) < 3:
                continue
            # Skip header / separator rows. Separators have only '-' / ':' / ' '.
            if all(set(c) <= set("-: ") for c in cells):
                continue
            # Heuristic: header rows typically contain words like 'project' /
            # 'name' / 'luid' / 'path'. Detect by lower-cased lookup.
            joined = " ".join(c.lower() for c in cells)
            if "project" in joined and "name" in joined and ("luid" in joined or "id" in joined):
                continue
            # Treat the first 3 cells as (project_path, name, luid). LUID is
            # the canonical 8-4-4-4-12 hex shape; only accept rows whose
            # third cell matches that pattern.
            luid_cell = cells[2]
            if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", luid_cell):
                continue
            rows.append({
                "project_path": cells[0],
                "name": cells[1],
                "luid": luid_cell,
            })
    return rows


def resolve_pds_luid(
    *,
    project_name: str | None,
    datasource_name: str | None,
    ds_index: list[dict[str, str]],
) -> dict[str, Any]:
    """Try to resolve a PDS LUID given the LoadSqlProxy's projectName +
    datasourceName.

    Returns a dict with status and details:
      - status='resolved': single match -> {'luid', 'project_path'}
      - status='ambiguous': 2+ matches -> {'candidates': [...]} so the LLM /
        user can disambiguate
      - status='unresolved': 0 matches -> {'reason': '...'}
    """
    if not datasource_name:
        return {"status": "unresolved", "reason": "Input node has no datasourceName"}

    name_matches = [r for r in ds_index if r["name"] == datasource_name]
    if not name_matches:
        return {
            "status": "unresolved",
            "reason": (
                f"no PDS named {datasource_name!r} found in deploy-context.md - "
                f"likely the PDS lives outside the scanned project(s); re-run "
                f"Phase B with --also-scan <parent-project-path>"
            ),
        }

    if project_name:
        # Match by project leaf name (LoadSqlProxy stores leaf, not full path).
        leaf_matches = [
            r for r in name_matches
            if r["project_path"].split("/")[-1] == project_name
        ]
        if len(leaf_matches) == 1:
            return {
                "status": "resolved",
                "luid": leaf_matches[0]["luid"],
                "project_path": leaf_matches[0]["project_path"],
            }
        if len(leaf_matches) > 1:
            return {"status": "ambiguous", "candidates": leaf_matches}
        # Fall through to ambiguous on full name_matches if no leaf match.

    if len(name_matches) == 1:
        return {
            "status": "resolved",
            "luid": name_matches[0]["luid"],
            "project_path": name_matches[0]["project_path"],
        }
    return {"status": "ambiguous", "candidates": name_matches}


def extract_fields_for_proposal(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-field summary used by the LLM to draft snake_case rename / cast /
    hide proposals. Filters out isGenerated=True (Tableau-injected columns
    that do not exist in the underlying table)."""
    out = []
    for f in node.get("fields") or []:
        if f.get("isGenerated"):
            continue
        out.append({
            "name_raw": f.get("name"),                         # uuid, no brackets
            "name_bracketed": f"[{f.get('name')}]",            # for spec.transforms[].column_name
            "caption": f.get("caption"),
            "datatype": f.get("type"),
        })
    return out


def classify_input(
    flow: dict[str, Any],
    nid: str,
    ds_index: list[dict[str, str]],
) -> dict[str, Any]:
    """Build the per-Input dispatch record."""
    node = flow["nodes"][nid]
    info = inspect_input_node(flow, nid)
    record: dict[str, Any] = {
        "node_id": nid,
        "node_name": node.get("name"),
        "kind": info["kind"],
        "node_type": info.get("node_type"),
        "fields": extract_fields_for_proposal(node),
    }

    if info["kind"] == "pds":
        ca = node.get("connectionAttributes") or {}
        record["pds"] = {
            "project_name": ca.get("projectName"),
            "datasource_name": ca.get("datasourceName"),
            "dbname": ca.get("dbname"),
            "resolution": resolve_pds_luid(
                project_name=ca.get("projectName"),
                datasource_name=ca.get("datasourceName"),
                ds_index=ds_index,
            ),
        }
    elif info["kind"] == "vconn":
        record["vconn"] = {
            "vconn_luid": info["vconn_luid"],
            "vconn_caption": info["vconn_caption"],
            "table_uuid": info["table_uuid"],
            "table_name": info["table_name"],
        }
        record["augmenter_columns_hint"] = vconn_input_to_augmenter_columns(node.get("fields") or [])
    elif info["kind"] == "direct_db":
        record["direct_db"] = {
            "connection_class": info.get("connection_class"),
            "node_type": info.get("node_type"),
        }
    return record


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--flow-json", required=True, help="Path to flow.json")
    ap.add_argument("--deploy-context", required=True,
                    help="Path to deploy-context.md (Phase B output). Used to "
                         "resolve PDS LUIDs by (projectName, datasourceName).")
    ap.add_argument("--output", required=True,
                    help="Path to write the mechanical findings JSON.")
    args = ap.parse_args()

    flow_path = Path(args.flow_json)
    ctx_path = Path(args.deploy_context)
    out_path = Path(args.output)
    if not flow_path.exists():
        print(f"[dispatch_inputs] ERROR: flow.json not found: {flow_path}", file=sys.stderr)
        return 1
    if not ctx_path.exists():
        print(
            f"[dispatch_inputs] WARNING: deploy-context.md not found at {ctx_path}; "
            f"all PDS Inputs will be reported as pds_luid_unresolved.",
            file=sys.stderr,
        )

    flow = json.loads(flow_path.read_text(encoding="utf-8"))
    ds_index = parse_deploy_context_datasources(ctx_path)
    print(
        f"[dispatch_inputs] indexed {len(ds_index)} datasource entries from "
        f"deploy-context.md",
        file=sys.stderr,
    )

    inputs = []
    pds_project_parents: set[str] = set()
    for nid, n in (flow.get("nodes") or {}).items():
        if n.get("baseType") != "input":
            continue
        rec = classify_input(flow, nid, ds_index)
        inputs.append(rec)
        if rec["kind"] == "pds":
            pname = (rec.get("pds") or {}).get("project_name")
            if pname:
                pds_project_parents.add(pname)

    payload = {
        "flow_path": str(flow_path),
        "deploy_context_path": str(ctx_path),
        "input_count": len(inputs),
        "kind_counts": {
            k: sum(1 for r in inputs if r["kind"] == k)
            for k in ("pds", "vconn", "direct_db", "extract", "unknown")
        },
        "pds_project_parents_needed_in_scope": sorted(pds_project_parents),
        "inputs": inputs,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"[dispatch_inputs] wrote {out_path} - {len(inputs)} input(s) classified",
        file=sys.stderr,
    )
    print(f"RESULT_JSON: {json.dumps({'output': str(out_path), 'input_count': len(inputs), 'kind_counts': payload['kind_counts']})}")

    unknown_inputs = [r for r in inputs if r["kind"] == "unknown"]
    if unknown_inputs:
        names = ", ".join(f"{r['node_name']!r} ({r.get('node_type')})" for r in unknown_inputs)
        print(
            f"[dispatch_inputs] ERROR: {len(unknown_inputs)} Input(s) classified "
            f"as 'unknown': {names}. This indicates the Skill premise is broken "
            f"(unsupported Prep version or malformed flow). Update flow_io."
            f"inspect_input_node before running architect.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
