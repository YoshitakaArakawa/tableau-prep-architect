#!/usr/bin/env python3
"""Emit a decomposition-plan.json skeleton with every mechanical field pre-filled.

The architect (LLM) then only edits DESIGN decisions — layer boundaries,
included_steps, splits, semantic rename translations, rename-back, migration
notes — instead of hand-writing LUIDs, project paths, server URLs, transform
tables, and output inventories (~45% of a hand-written plan by volume).

Pre-filled from machine sources:
  - server / target project LUIDs .... deploy-context.md (Phase B)
  - original flow outputs ............ source flow.json PublishExtract nodes
  - Input classification ............. input-dispatch-mech.json (Phase B)
      vconn        -> kind=pds_augment stg entry, transforms table pre-filled
                      (op=rename, to_caption = current caption — TRANSLATE these)
      pds resolved -> `_passthrough_hints` block (copy into downstream
                      entries' inputs[] as kind=passthrough_pds)
      direct_db /
      extract      -> stg entry with input_status=needs_provisioning
  - step numbering ................... canonical bfs_order (same numbers as
                                       flow-summary.md's Topology table)

Usage:
    python gen_plan_skeleton.py \
        --source <original>.tfl \
        --input-dispatch reports/input-dispatch-mech.json \
        --deploy-context reports/deploy-context.md \
        --out reports/decomposition-plan-<flow>.json \
        [--flow-name <name>] [--original-flow-luid <luid>] \
        [--site-url-name <content-url>]   # default: SITE_NAME from repo .env
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from flow_io import load_flow_json  # noqa: E402
from plan_model import StepResolver, parse_deploy_context  # noqa: E402

NODE_TYPE_PUBLISH_EXTRACT = ".v1.PublishExtract"


def read_source(path: Path) -> dict:
    if path.suffix in (".tfl", ".tflx"):
        return load_flow_json(path)
    return json.loads(path.read_text(encoding="utf-8"))


def site_url_name_from_env() -> str:
    env = REPO_ROOT / ".env"
    if env.exists():
        m = re.search(r"^SITE_NAME\s*=\s*(.+)$", env.read_text(encoding="utf-8"),
                      re.MULTILINE)
        if m:
            return m.group(1).strip().strip('"').strip("'")
    return ""


def snake(name: str) -> str:
    s = re.sub(r"[^0-9A-Za-z]+", "_", name).strip("_").lower()
    return s or "source"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--source", required=True, type=Path,
                   help="Original .tfl/.tflx or extracted flow.json")
    p.add_argument("--input-dispatch", required=True, type=Path,
                   help="input-dispatch-mech.json (Phase B)")
    p.add_argument("--deploy-context", required=True, type=Path,
                   help="deploy-context.md (Phase B)")
    p.add_argument("--out", required=True, type=Path,
                   help="Where to write the plan.json skeleton")
    p.add_argument("--flow-name", default=None)
    p.add_argument("--original-flow-luid", default=None)
    p.add_argument("--site-url-name", default=None)
    args = p.parse_args()

    flow = read_source(args.source)
    resolver = StepResolver(flow)
    dispatch = json.loads(args.input_dispatch.read_text(encoding="utf-8"))
    ctx = parse_deploy_context(args.deploy_context)

    missing_layers = [
        f"{grp}.{layer}"
        for grp in ("flow_projects", "ds_projects")
        for layer in ("staging", "intermediate", "marts")
        if layer not in ctx[grp]
    ]
    if missing_layers:
        print(
            "[gen_plan_skeleton] WARNING: deploy-context is missing layer "
            f"project(s): {missing_layers} — run prep-deployer preflight first, "
            "then re-run Phase B. Emitting TODO placeholders.",
            file=sys.stderr,
        )

    flow_name = args.flow_name or args.source.stem
    outputs = [
        {"name": n.get("datasourceName"), "luid": None}
        for n in flow.get("nodes", {}).values()
        if n.get("nodeType") == NODE_TYPE_PUBLISH_EXTRACT and n.get("datasourceName")
    ]

    def layer_projects(grp: str) -> dict:
        return {
            layer: ctx[grp].get(layer, {"path": "TODO_run_preflight", "luid": "TODO"})
            for layer in ("staging", "intermediate", "marts")
        }

    flows: list[dict] = []
    passthrough_hints: list[dict] = []
    for inp in dispatch.get("inputs", []):
        step = resolver.step_by_uuid.get(inp["node_id"])
        if step is None:
            print(f"[gen_plan_skeleton] WARNING: dispatch node_id "
                  f"{inp['node_id']} not in source flow — skipped",
                  file=sys.stderr)
            continue
        kind = inp.get("kind")
        if kind == "vconn":
            cols = inp.get("augmenter_columns_hint") or []
            flows.append({
                "name": f"stg_{snake(inp.get('node_name', 'source'))}",
                "layer": "staging",
                "kind": "pds_augment",
                "source_input_step": step,
                "table_name": (inp.get("vconn") or {}).get("table_name"),
                "transforms": [
                    {"op": "rename", "column_name": c["name"],
                     "to_caption": c["caption"]}
                    for c in cols
                ],
                "description": "TODO: 1-2 lines",
                "_todo": "Rename this stg; TRANSLATE to_caption values "
                         "(semantic English snake_case for non-ASCII captions); "
                         "drop transforms for columns kept as-is; add cast/hide "
                         "ops only if the design needs them.",
            })
        elif kind == "pds":
            res = (inp.get("pds") or {}).get("resolution") or {}
            passthrough_hints.append({
                "pds_name": (inp.get("pds") or {}).get("datasource_name"),
                "project_path": res.get("project_path"),
                "luid": res.get("luid"),
                "dbname": (inp.get("pds") or {}).get("dbname"),
                "resolution_status": res.get("status"),
                "source_input_step": step,
                "_usage": "copy into a downstream entry's inputs[] as "
                          '{"kind": "passthrough_pds", ..., "replaces_steps": '
                          f"[{step}]}} — or use {{\"kind\": \"transplant\", "
                          f"\"step\": {step}}} to carry the original node verbatim",
            })
        elif kind in ("direct_db", "extract"):
            flows.append({
                "name": f"stg_{snake(inp.get('node_name', 'source'))}",
                "layer": "staging",
                "kind": "tfl",
                "input_status": "needs_provisioning",
                "included_steps": [],
                "inputs": [],
                "output": {"name": f"stg_{snake(inp.get('node_name', 'source'))}"},
                "provisioning": {
                    "source": inp.get("node_name"),
                    "kind": kind,
                    "connection_class": (inp.get(kind) or {}).get("connection_class"),
                    "recommendation": "TODO: provisioning proposal",
                    "resume": "Phase A から再開 (flow 自体が変わるため)",
                },
                "description": "TODO",
            })

    skeleton = {
        "schema_version": "1",
        "flow_name": flow_name,
        "source": {
            "tfl_path": str(args.source).replace("\\", "/"),
            "total_nodes": len(resolver.order),
        },
        "server": {
            "url": ctx["server"],
            "site_url_name": args.site_url_name or site_url_name_from_env(),
        },
        "original": {
            "flow_luid": args.original_flow_luid,
            "outputs": outputs,
        },
        "flow_projects": layer_projects("flow_projects"),
        "ds_projects": layer_projects("ds_projects"),
        "flows": flows,
        "_passthrough_hints": passthrough_hints,
        "_design_todo": [
            "Add kind=tfl entries for int/marts (included_steps, inputs, output, "
            "description; splits / rename_back / joins / incremental as needed). "
            "Schema: references/plan-json-schema.md",
            "Set source_original_output_name on every entry that inherits an "
            "original output (drives Output mapping + comparator pairing)",
            "Marts inheriting an original output need rename_back so the output "
            "schema matches the original PDS exactly",
            "When done: delete every _-prefixed key, then run render_plan_md.py "
            "(it validates the plan and writes the Stop-2 review markdown)",
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(skeleton, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"[gen_plan_skeleton] wrote {args.out} — "
        f"{len(flows)} pre-filled stg entr(ies), "
        f"{len(passthrough_hints)} passthrough hint(s), "
        f"{len(outputs)} original output(s), {len(resolver.order)} source nodes",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
