#!/usr/bin/env python3
"""Materialize a decomposition plan.json into .tfl files + augmenter specs.

Replaces the per-session hand-written build_tfls.py: the plan.json emitted by
tableau-prep-architect (schema: references/plan-json-schema.md) carries every design
decision, and this script performs the mechanical assembly with the proven
flow_io primitives. Wiring (split remap, excluded-node bridging, input
substitution, namespace inheritance) is computed by scripts/plan_model.py's
compute_flow_graph — the same implementation render_plan_md.py validated
against at Stop 2, so what the user approved is what gets built.

Outputs (under --output-dir, layer subdirs created as needed):
  flows/staging/<name>.tfl | <name>.augmenter.json
  flows/intermediate/<name>.tfl
  flows/marts/<name>.tfl

Verification (all hard gates): plan structural validation, plan-vs-source
validation, verify_lineage_closure, verify_edge_namespaces, zip entry check.

Usage:
    python build_from_plan.py --plan reports/decomposition-plan-<flow>.json \
        --source <original>.tfl --output-dir <session>/flows \
        [--only name1 --only name2]          # targeted rebuild (gap fix)
        [--manifest reports/publish-manifest.json [--force-manifest]]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from flow_io import (  # noqa: E402
    PUBLISHABLE_AUX_ENTRIES,
    add_pds_input,
    copy_source_node,
    inspect_input_node,
    load_aux_entries,
    load_flow_json,
    make_publish_extract_node,
    make_rename_supertransform,
    normalize_source_containers,
    pack_flow_json,
    set_incremental_refresh,
    vconn_input_to_augmenter_columns,
    verify_edge_namespaces,
    verify_lineage_closure,
)
from build_helpers import (  # noqa: E402
    empty_flow,
    split_supertransform_actions,
    transplant_source_input,
)
from plan_model import (  # noqa: E402
    StepResolver,
    augment_output_fields,
    compute_flow_graph,
    load_plan,
    validate_plan_with_source,
)

LAYER_DIR = {"staging": "staging", "intermediate": "intermediate", "marts": "marts"}
PUBLISH_MANIFEST_PY = REPO_ROOT / "scripts" / "publish_manifest.py"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build .tfl files / augmenter specs from a decomposition plan.json"
    )
    p.add_argument("--plan", required=True, help="Path to decomposition-plan-<flow>.json")
    p.add_argument("--source", required=True, help="Path to the original .tfl/.tflx")
    p.add_argument("--output-dir", required=True,
                   help="flows/ directory to write into (layer subdirs created)")
    p.add_argument("--only", action="append", default=None,
                   help="Rebuild only the named flow(s) (repeatable). "
                        "Skips manifest init — use for targeted gap fixes.")
    p.add_argument("--overwrite", action="store_true",
                   help="Allow overwriting existing .tfl/.augmenter.json outputs. "
                        "Implied for flows named via --only (a rebuild IS an overwrite).")
    p.add_argument("--manifest", default=None,
                   help="Also run publish_manifest.py init --plan-json to this path")
    p.add_argument("--force-manifest", action="store_true",
                   help="Pass --force to manifest init (overwrites publish/run state)")
    return p.parse_args()


def build_augment_spec(entry: dict, resolver: StepResolver, plan: dict,
                       out_dir: Path) -> Path:
    """kind=pds_augment: emit flows/staging/<name>.augmenter.json."""
    name = entry["name"]
    info = inspect_input_node(resolver.flow, resolver.uuid(entry["source_input_step"]))
    if info["kind"] != "vconn":
        raise SystemExit(
            f"[build] ERROR: {name}: source_input_step {entry['source_input_step']} "
            f"is kind={info['kind']!r}, expected vconn. live_pds materialization is "
            "vconn-only — fix the plan (decompose self-check) or provision the input."
        )
    expect_table = entry.get("table_name")
    if expect_table and info.get("table_name") != expect_table:
        raise SystemExit(
            f"[build] ERROR: {name}: table_name mismatch — plan expects "
            f"{expect_table!r}, source input reads {info.get('table_name')!r}"
        )
    columns = vconn_input_to_augmenter_columns(info["fields"])
    known = {c["name"] for c in columns}
    missing = [t["column_name"] for t in entry.get("transforms", [])
               if t["column_name"] not in known]
    if missing:
        raise SystemExit(
            f"[build] ERROR: {name}: transform column(s) not present on the "
            f"source input: {missing}"
        )
    spec = {
        "source": {
            "kind": "vconn",
            "vconn_luid": info["vconn_luid"],
            "vconn_caption": info["vconn_caption"],
            "table_uuid": info["table_uuid"],
            "table_name": info["table_name"],
            "columns": columns,
        },
        "target": {
            "project_id": plan["ds_projects"]["staging"]["luid"],
            "new_name": name,
        },
        "mode": "CreateNew",
        "transforms": entry.get("transforms", []),
    }
    path = out_dir / "staging" / f"{name}.augmenter.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return path


def upstream_lsp_fields(pds_name: str, plan: dict, resolver: StepResolver) -> list[dict]:
    """Schema for an LSP reading a PDS produced by this plan: statically known
    only when the upstream is a pds_augment (columns = input fields with
    transforms applied); a tfl upstream's output schema emerges at run time,
    so pass [] and let the server fill it."""
    for f in plan.get("flows", []):
        if f["name"] == pds_name and f.get("kind") == "pds_augment":
            return augment_output_fields(f, resolver)
    return []


def build_tfl(entry: dict, resolver: StepResolver, plan: dict,
              aux: dict[str, bytes], out_dir: Path) -> tuple[Path, list[str]]:
    """kind=tfl: assemble one new flow from the plan entry's wiring graph.

    Returns (tfl_path, verify_issues). verify_issues non-empty == broken build
    (caller aborts; the file is still written for inspection).
    """
    name = entry["name"]
    layer = entry["layer"]
    src = resolver.flow
    server_url = plan["server"]["url"]
    site = plan["server"]["site_url_name"]

    graph = compute_flow_graph(entry, resolver, plan)
    if graph.issues:
        raise SystemExit(
            f"[build] ERROR: {name}: plan wiring is not buildable:\n  - "
            + "\n  - ".join(graph.issues)
        )

    flow = empty_flow(name)
    node_id_by_key: dict[str, str] = {}
    parent_substitutions: dict[str, str] = {}
    synthetic_lineage: dict[str, list[str]] = {}

    # --- materialize nodes
    for key, gn in graph.nodes.items():
        if gn.role == "included":
            # keep verbatim edges to direct (non-bridged) included children;
            # everything else is appended from the graph below.
            direct_children = {
                graph.nodes[e.dst].source_uuid
                for e in graph.edges
                if e.src == key and not e.bridged_over
                and graph.nodes[e.dst].role == "included"
            }
            flow["nodes"][gn.source_uuid] = copy_source_node(
                src, gn.source_uuid, kept_children=direct_children)
            node_id_by_key[key] = gn.source_uuid
        elif gn.role == "split":
            sp = next(s for s in entry.get("splits", []) or []
                      if s["step"] == gn.step)
            new_id = str(uuid.uuid4())
            flow["nodes"][new_id] = split_supertransform_actions(
                src["nodes"][gn.source_uuid], sp["action_indices"],
                new_name=sp["new_name"], new_id=new_id)
            node_id_by_key[key] = new_id
        elif gn.role == "transplant":
            nid = transplant_source_input(flow, src, gn.source_uuid)
            node_id_by_key[key] = nid
        elif gn.role == "lsp":
            inp = entry["inputs"][gn.input_index]
            if inp["kind"] == "upstream_pds":
                up_layer = next((f["layer"] for f in plan["flows"]
                                 if f["name"] == inp["pds_name"]), "staging")
                project_name = plan["ds_projects"][up_layer]["path"]
                dbname = None  # placeholder; deployer patches after upstream run
                fields = upstream_lsp_fields(inp["pds_name"], plan, resolver)
            else:  # passthrough_pds — pre-existing PDS at a known project path.
                # Emit the FULL path (not the leaf). The deployer's dbname-patch
                # tooling (discover_pds_dbname / auto_patch_downstream) matches
                # LoadSqlProxy nodes by EXACT projectName against the full path,
                # so a leaf here makes those patches silently match nothing
                # (observed 20260712 #7). A full-path projectName publishes and
                # runs on Cloud — the upstream_pds branch above has always
                # emitted full paths that publish + run.
                project_name = inp["project_path"]
                dbname = inp.get("dbname")
                fields = []
                if not dbname:
                    print(
                        f"[build] WARNING: {name}: passthrough_pds input "
                        f"{inp['pds_name']!r} has no dbname in the plan — "
                        "emitting a placeholder. The deployer must resolve + "
                        "patch it before run (auto_patch_downstream); this now "
                        "works because projectName is the full path. Add the "
                        "input's dbname to the plan (input-dispatch pds.dbname) "
                        "to skip that round-trip entirely.",
                        file=sys.stderr,
                    )
            lsp_id, _ = add_pds_input(
                flow, server_url=server_url, site_url_name=site,
                project_name=project_name, datasource_name=inp["pds_name"],
                dbname=dbname, fields=fields, name=inp["pds_name"],
            )
            node_id_by_key[key] = lsp_id
            r_uuids = [resolver.uuid(s) for s in inp.get("replaces_steps") or []]
            if r_uuids:
                parent_substitutions[lsp_id] = r_uuids[0]
                synthetic_lineage.setdefault(inp["pds_name"], []).extend(r_uuids)

    # --- edges (namespaces already resolved by compute_flow_graph)
    # Dedupe key includes the namespace: two edges between the same pair with
    # DIFFERENT namespaces are legitimate (self-union feeding one SuperUnion
    # through two branches) and must both survive.
    existing: set[tuple[str, str, str]] = set()
    for nid, n in flow["nodes"].items():
        for nx in n.get("nextNodes", []) or []:
            existing.add((nid, nx.get("nextNodeId"),
                          nx.get("nextNamespace") or "Default"))
    for e in graph.edges:
        src_id = node_id_by_key[e.src]
        dst_id = node_id_by_key[e.dst]
        if (src_id, dst_id, e.ns) in existing:
            continue  # verbatim-kept edge from copy_source_node
        flow["nodes"][src_id].setdefault("nextNodes", []).append({
            "namespace": "Default",
            "nextNodeId": dst_id,
            "nextNamespace": e.ns,
        })

    # --- output (optionally via rename-back)
    ds_proj = plan["ds_projects"][layer]
    out_node = make_publish_extract_node(
        project_name=ds_proj["path"], project_luid=ds_proj["luid"],
        datasource_name=entry["output"]["name"],
        server_url=server_url, site_url_name=site, name="Output",
        description=entry.get("description", ""),
    )
    flow["nodes"][out_node["id"]] = out_node
    sink_id = node_id_by_key[graph.sink_key]
    renames = [(rb["from"], rb["to"]) for rb in entry.get("rename_back", []) or []]
    attach_to = sink_id
    if renames:
        rb_node = make_rename_supertransform(renames=renames)
        flow["nodes"][rb_node["id"]] = rb_node
        flow["nodes"][sink_id].setdefault("nextNodes", []).append({
            "namespace": "Default", "nextNodeId": rb_node["id"],
            "nextNamespace": "Default",
        })
        attach_to = rb_node["id"]
    flow["nodes"][attach_to].setdefault("nextNodes", []).append({
        "namespace": "Default", "nextNodeId": out_node["id"],
        "nextNamespace": "Default",
    })

    # --- initialNodes / nodeProperties
    input_ids = [node_id_by_key[k] for k, gn in graph.nodes.items()
                 if gn.role in ("lsp", "transplant")]
    flow["initialNodes"] = input_ids
    flow["nodeProperties"] = {nid: {} for nid in input_ids}

    # --- incremental refresh (optional)
    inc = entry.get("incremental")
    if inc:
        ref = inc["input"]
        input_node_id = None
        for k, gn in graph.nodes.items():
            if gn.role == "lsp" and entry["inputs"][gn.input_index]["pds_name"] == ref:
                input_node_id = node_id_by_key[k]
            elif gn.role == "transplant" and gn.step == ref:
                input_node_id = node_id_by_key[k]
        if input_node_id is None:
            raise SystemExit(
                f"[build] ERROR: {name}: incremental.input {ref!r} matches no "
                "input (use an input pds_name, or a transplant step number)"
            )
        set_incremental_refresh(
            flow, input_node_id=input_node_id,
            control_field=inc["control_field"],
            output_node_id=out_node["id"],
            output_field=inc["output_field"],
            is_incremental_default=inc.get("is_incremental_default", True),
        )

    # --- write + verify
    path = out_dir / LAYER_DIR[layer] / f"{name}.tfl"
    path.parent.mkdir(parents=True, exist_ok=True)
    pack_flow_json(flow, path, aux_entries=aux)

    bridged = {
        (node_id_by_key[e.src], node_id_by_key[e.dst], e.ns)
        for e in graph.edges if e.bridged_over
    }
    issues = verify_lineage_closure(flow, src,
                                    synthetic_input_lineage=synthetic_lineage)
    issues += verify_edge_namespaces(flow, src,
                                     parent_substitutions=parent_substitutions,
                                     bridged_edges=bridged)
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
    issues += [f"zip missing entry {k!r}" for k in ("flow", "maestroMetadata")
               if k not in names]
    return path, issues


def find_placeholder_projects(plan: dict) -> list[str]:
    """Return group.layer keys whose path/luid are still gen_plan_skeleton's
    TODO placeholders. Placeholders present == the plan predates
    preflight + Phase B re-run (migration-workflow step 4), so none of its
    baked LUIDs are real."""
    found = []
    for grp in ("flow_projects", "ds_projects"):
        for layer, ref in (plan.get(grp) or {}).items():
            vals = (ref.get("path", ""), ref.get("luid", ""))
            if any(str(v).startswith("TODO") for v in vals):
                found.append(f"{grp}.{layer}")
    return found


def main() -> int:
    args = parse_args()
    plan = load_plan(args.plan)
    out_dir = Path(args.output_dir)

    placeholders = find_placeholder_projects(plan)
    if placeholders:
        if args.manifest:
            print("[build] ERROR: plan still contains TODO placeholder project "
                  f"LUIDs ({', '.join(placeholders)}) — a --manifest build is "
                  "publish-bound and would bake unpublishable Outputs.\n"
                  "  Fix: run tableau-prep-deployer preflight, re-run tableau-prep-extractor "
                  "Phase B (migration-workflow step 4), then re-run "
                  "gen_plan_skeleton or copy the "
                  "layer LUIDs into the plan's flow_projects/ds_projects.\n"
                  "  Or: drop --manifest for a local-only (goal 3) build.",
                  file=sys.stderr)
            return 1
        print("[build] WARNING: TODO placeholder project LUIDs in "
              f"{', '.join(placeholders)} — outputs are local-only and NOT "
              "publishable (goal 3). Promote to publish via preflight -> "
              "Phase B re-run -> plan LUID update -> rebuild with --manifest.")

    source = load_flow_json(args.source)
    source, skipped = normalize_source_containers(source)
    if skipped:
        print(f"[build] WARNING: non-convertible containers left verbatim: {skipped}")
    aux = load_aux_entries(args.source, names=PUBLISHABLE_AUX_ENTRIES)
    if "maestroMetadata" not in aux:
        print("[build] ERROR: source .tfl has no maestroMetadata — "
              "publish would fail with 280003.", file=sys.stderr)
        return 1

    resolver = StepResolver(source)
    issues, notes = validate_plan_with_source(plan, source)
    for n in notes:
        print(f"[plan note] {n}")
    if issues:
        print(f"[build] ERROR: plan validation failed ({len(issues)}):",
              file=sys.stderr)
        for i in issues:
            print(f"  - {i}", file=sys.stderr)
        return 1

    selected = plan["flows"]
    if args.only:
        unknown = set(args.only) - {f["name"] for f in selected}
        if unknown:
            print(f"[build] ERROR: --only names not in plan: {sorted(unknown)}",
                  file=sys.stderr)
            return 1
        selected = [f for f in selected if f["name"] in args.only]

    built: list[str] = []
    skipped_prov: list[str] = []
    all_ok = True
    for entry in selected:
        name = entry["name"]
        if entry.get("input_status") == "needs_provisioning":
            skipped_prov.append(name)
            print(f"[build] SKIP {name}: needs_provisioning "
                  "(see plan's Input provisioning required)")
            continue
        if entry["kind"] == "pds_augment":
            target = out_dir / "staging" / f"{name}.augmenter.json"
        else:
            target = out_dir / LAYER_DIR[entry["layer"]] / f"{name}.tfl"
        if target.exists() and not args.overwrite and not args.only:
            print(f"[build] ERROR: {target} already exists — pass --overwrite "
                  "to replace, or --only for a targeted rebuild.", file=sys.stderr)
            return 1
        if entry["kind"] == "pds_augment":
            path = build_augment_spec(entry, resolver, plan, out_dir)
            print(f"[build] ok  {path}")
        else:
            path, v_issues = build_tfl(entry, resolver, plan, aux, out_dir)
            if v_issues:
                all_ok = False
                print(f"[build] VERIFY FAILED {path}:", file=sys.stderr)
                for i in v_issues:
                    print(f"  - {i}", file=sys.stderr)
            else:
                print(f"[build] ok  {path}")
        built.append(name)

    if not all_ok:
        return 1

    if args.manifest and not args.only:
        cmd = [sys.executable, str(PUBLISH_MANIFEST_PY), "init",
               "--plan-json", args.plan, "--flows-dir", str(out_dir),
               "--output", args.manifest]
        if args.force_manifest:
            cmd.append("--force")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        sys.stderr.write(proc.stderr)
        if proc.returncode != 0:
            return proc.returncode

    n_aug = sum(1 for f in selected if f["kind"] == "pds_augment"
                and f["name"] in built)
    print(f"[build] done: {len(built)} artifact(s) "
          f"({len(built) - n_aug} tfl / {n_aug} augment), "
          f"{len(skipped_prov)} skipped pending provisioning{skipped_prov or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
