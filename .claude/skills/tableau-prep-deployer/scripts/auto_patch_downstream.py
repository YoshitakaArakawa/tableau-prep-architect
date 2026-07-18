#!/usr/bin/env python3
"""Patch placeholder dbnames in every .tfl that references an already-run PDS.

tableau-prep-builder writes each cross-layer / cross-flow Input (LoadSqlProxy) with a
placeholder dbname (`<datasourceName>_placeholder`). Publish accepts the
placeholder, but flow run requires the real Cloud-assigned physical Hyper
name, so each downstream .tfl has to be patched before its own run.

Until now the operator called `discover_pds_dbname.py --patch` once per
(upstream PDS x downstream .tfl) pair — six times in the 20260519 session.
This script collapses that into one invocation per checkpoint.

Pipeline:
  1. Read publish-manifest. Collect every `outputs[].name` whose run.status
     == success. Remember each PDS's source layer (we need it to construct
     the upstream PDS's projectName on Cloud).
  2. Scan every .tfl under `<flows_dir>/{staging,intermediate,marts}/*.tfl`.
     For each LoadSqlProxy whose datasourceName is in the ready set, mark
     the (.tfl, datasourceName) pair for patching. This naturally covers
     stg->int, int->marts, AND intra-layer cross-refs (e.g. an intermediate
     .tfl that reads another intermediate PDS).
  3. Resolve the real dbname for each ready PDS via Cloud (Metadata API
     path, reusing discover_pds_dbname.discover()) and patch in place with
     flow_io.patch_pds_dbname.

Idempotent: patching with the same dbname is a no-op. Safe to call after
every `run_layer.py` invocation — the script will only resolve PDSes that
are actually referenced by some unpatched .tfl.

Usage:

    # After each layer run completes (or before publishing a layer that
    # references same-layer PDSes), call:
    python auto_patch_downstream.py \
      --manifest work/<session>/reports/publish-manifest.json \
      --flows-dir work/<session>/flows \
      --target-path "99_Sandbox/flow241407_decompose"

`--target-path` is the path of the **target project** on Cloud that holds
the `datasources/{stg,intermediate,marts}` subprojects (split layout
introduced in commit 2d83cfa). The upstream PDS lookup path is derived
as `<target_path>/datasources/<cloud_layer_name>` where `cloud_layer_name`
maps manifest's `staging` -> `stg` and passes `intermediate` / `marts`
through unchanged.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from flow_io import load_aux_entries, load_flow_json, pack_flow_json, patch_pds_dbname  # noqa: E402
from tableau_auth import signed_in_server  # noqa: E402

import discover_pds_dbname  # noqa: E402


LAYER_DIRS = ("staging", "intermediate", "marts")

# Manifest layer label -> Cloud subproject name under <target>/datasources/.
# Manifest stores the long form ("staging") to match local flows/ subdirs and
# tableau-prep-architect's analysis docs; Cloud uses the short form ("stg") for the
# datasources/<layer> project name per references/naming-conventions.md.
MANIFEST_LAYER_TO_CLOUD_DS_LAYER = {
    "staging": "stg",
    "intermediate": "intermediate",
    "marts": "marts",
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Patch downstream .tfl dbnames from manifest run state in one shot"
    )
    p.add_argument("--manifest", required=True, help="Path to publish-manifest.json")
    p.add_argument("--flows-dir", required=True,
                   help="Path to the flows/ directory containing staging/, intermediate/, marts/")
    p.add_argument("--target-path", required=True,
                   help="Path of the target project on Cloud that holds "
                        "datasources/{stg,intermediate,marts} "
                        "(e.g. '99_Sandbox/flow241407_decompose'). The upstream PDS "
                        "lookup path is derived as "
                        "'<target-path>/datasources/<cloud_layer_name>', where "
                        "manifest's 'staging' maps to Cloud's 'stg' "
                        "(intermediate / marts pass through unchanged).")
    p.add_argument("--use-candidate", default="content_url",
                   help="Which discover_pds_dbname candidate to use as dbname "
                        "(default: content_url)")
    return p.parse_args()


def ready_pdses(manifest: dict) -> dict[str, str]:
    """Return a dict {pds_name: layer} for every PDS that exists on Cloud.

    Two ways an entry's PDS becomes ready:
    - kind=tfl:         its flow ran successfully (run.status == success) —
                        the PDS is (re)materialized by the run.
    - kind=pds_augment: the Live PDS exists as soon as publish succeeds
                        (publish.status == published); run stays "n/a".
    """
    out: dict[str, str] = {}
    for df in manifest.get("decomposed_flows", []):
        if df.get("kind") == "pds_augment":
            if (df.get("publish") or {}).get("status") != "published":
                continue
        elif (df.get("run") or {}).get("status") != "success":
            continue
        layer = df.get("layer")
        if not layer:
            continue
        for o in df.get("outputs") or []:
            name = o.get("name")
            if name and name not in out:
                out[name] = layer
    return out


def all_tfls(flows_dir: Path) -> list[Path]:
    """Return every .tfl under flows_dir/{staging,intermediate,marts}/."""
    out: list[Path] = []
    for layer in LAYER_DIRS:
        sub = flows_dir / layer
        if sub.is_dir():
            out.extend(sorted(sub.glob("*.tfl")))
    return out


def scan_refs(tfl_path: Path, ready: dict[str, str]) -> set[str]:
    """Return the subset of ready PDSes that this .tfl references via LoadSqlProxy."""
    flow = load_flow_json(tfl_path)
    refs: set[str] = set()
    for node in (flow.get("nodes") or {}).values():
        if not node.get("nodeType", "").endswith("LoadSqlProxy"):
            continue
        ds = (node.get("connectionAttributes") or {}).get("datasourceName")
        if ds in ready:
            refs.add(ds)
    return refs


def resolve_dbname(server, *, datasource_name: str, project_path: str, use_candidate: str,
                   projects_cache: list | None = None) -> str:
    result = discover_pds_dbname.discover(
        server,
        datasource_name=datasource_name,
        project_path=project_path,
        projects_cache=projects_cache,
    )
    dbname = result["candidates"].get(use_candidate)
    if not dbname:
        sys.exit(
            f"ERROR: dbname candidate '{use_candidate}' empty for "
            f"datasource='{datasource_name}' in project='{project_path}'. "
            f"Full discovery result:\n{json.dumps(result, indent=2, ensure_ascii=False)}"
        )
    return dbname


def patch_one_tfl(tfl_path: Path, *, refs: set[str], dbnames: dict[str, str],
                  pds_project: dict[str, str]) -> int:
    """Patch every (LoadSqlProxy, dataConnection) pair in refs. Returns node count."""
    flow = load_flow_json(tfl_path)
    aux = load_aux_entries(tfl_path)
    total = 0
    for ds in sorted(refs):
        n = patch_pds_dbname(
            flow,
            datasource_name=ds,
            project_name=pds_project[ds],
            dbname=dbnames[ds],
        )
        total += n
    pack_flow_json(flow, tfl_path, aux_entries=aux)
    return total


def main() -> int:
    args = parse_args()
    manifest_path = Path(args.manifest)
    flows_dir = Path(args.flows_dir)
    target_path = args.target_path.rstrip("/")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ready = ready_pdses(manifest)

    if not ready:
        print(
            "[auto_patch] No successfully-run flows in manifest. Nothing to patch.",
            file=sys.stderr,
        )
        return 0

    unknown_layers = {layer for layer in ready.values() if layer not in MANIFEST_LAYER_TO_CLOUD_DS_LAYER}
    if unknown_layers:
        sys.exit(
            f"ERROR: manifest contains unknown layer(s) {sorted(unknown_layers)}; "
            f"expected one of {sorted(MANIFEST_LAYER_TO_CLOUD_DS_LAYER)}. "
            f"Update MANIFEST_LAYER_TO_CLOUD_DS_LAYER in auto_patch_downstream.py."
        )

    pds_project = {
        ds: f"{target_path}/datasources/{MANIFEST_LAYER_TO_CLOUD_DS_LAYER[layer]}"
        for ds, layer in ready.items()
    }

    print(f"[auto_patch] {len(ready)} ready PDS(es) in manifest:")
    for ds, layer in sorted(ready.items()):
        print(f"  - {ds} (layer={layer}, project={pds_project[ds]})")

    tfl_paths = all_tfls(flows_dir)
    if not tfl_paths:
        print(f"[auto_patch] No .tfl files under {flows_dir}. Nothing to patch.", file=sys.stderr)
        return 0

    plan: list[tuple[Path, set[str]]] = []
    needed: set[str] = set()
    for tfl in tfl_paths:
        refs = scan_refs(tfl, ready)
        if not refs:
            continue
        plan.append((tfl, refs))
        needed |= refs

    if not plan:
        print(
            "[auto_patch] No .tfl references any of the ready PDSes. Nothing to patch.",
            file=sys.stderr,
        )
        return 0

    print(
        f"\n[auto_patch] resolving {len(needed)} unique dbname(s) from Cloud..."
    )

    with signed_in_server() as server:
        projects_cache = discover_pds_dbname.fetch_all_projects(server)
        dbnames: dict[str, str] = {}
        for ds in sorted(needed):
            dbname = resolve_dbname(
                server,
                datasource_name=ds,
                project_path=pds_project[ds],
                use_candidate=args.use_candidate,
                projects_cache=projects_cache,
            )
            dbnames[ds] = dbname
            print(f"  [ok] {ds} -> dbname={dbname!r}")

    total_pairs = 0
    print(f"\n[auto_patch] patching {len(plan)} .tfl file(s)...")
    for tfl, refs in plan:
        n = patch_one_tfl(tfl, refs=refs, dbnames=dbnames, pds_project=pds_project)
        total_pairs += n
        print(f"  [ok] {tfl.name}: patched {n} node-pair(s) for {sorted(refs)}")

    print(
        f"\n[auto_patch] done. {total_pairs} (LoadSqlProxy, dataConnection) "
        f"pair(s) updated across {len(plan)} .tfl file(s)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
