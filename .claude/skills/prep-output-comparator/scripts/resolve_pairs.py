#!/usr/bin/env python3
"""Resolve original <-> new published-datasource pairs from a session manifest.

Reads work/<session>/reports/publish-manifest.json (written by prep-builder and
enriched by prep-deployer) and emits a pairs.json that prep-output-comparator's
fork agent consumes to drive schema + row-count comparisons.

The manifest is the source of truth for both the original-to-decomposed name
mapping (decomposed_flows[].source_original_output_name) and the PDS LUIDs.
This script does not contact the Metadata API — if LUIDs are missing, it errors
out asking the caller to run `publish_manifest.py resolve-luids` first.

Usage:
    python resolve_pairs.py --manifest <path> --output <output_dir>/pairs.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


def jst_now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds")


def build_pairs(manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Compose pair entries from manifest.

    Returns (pairs, warnings). Each pair = {pair_index, original, new}.
    """
    warnings: list[str] = []

    if not manifest["original"].get("flow_luid"):
        warnings.append(
            "original.flow_luid is null — run "
            "`python scripts/publish_manifest.py resolve-luids` first"
        )

    # Build lookup: original output PDS name -> {luid}
    orig_by_name: dict[str, dict[str, Any]] = {}
    for o in manifest["original"].get("outputs") or []:
        orig_by_name[o["name"]] = o

    pairs: list[dict[str, Any]] = []
    pair_idx = 0

    for df in manifest.get("decomposed_flows") or []:
        src_name = df.get("source_original_output_name")
        if not src_name:
            # stg/int Hyper-only flows are not part of parity comparison.
            continue

        orig = orig_by_name.get(src_name)
        if orig is None:
            warnings.append(
                f"decomposed flow '{df['name']}' references "
                f"source_original_output_name='{src_name}' not present in "
                f"manifest.original.outputs"
            )
            continue

        new_outs = df.get("outputs") or []
        if not new_outs:
            warnings.append(
                f"decomposed flow '{df['name']}' has no PublishExtract outputs"
            )
            continue
        if len(new_outs) > 1:
            warnings.append(
                f"decomposed flow '{df['name']}' has {len(new_outs)} outputs; "
                "pairing with the first one only"
            )
        new_out = new_outs[0]

        if not orig.get("luid"):
            warnings.append(
                f"original output PDS '{src_name}' has null LUID — "
                "run resolve-luids on the manifest first"
            )
        if not new_out.get("luid"):
            warnings.append(
                f"decomposed output PDS '{new_out['name']}' "
                f"(from flow '{df['name']}') has null LUID — "
                "run resolve-luids on the manifest first"
            )

        pairs.append({
            "pair_index": pair_idx,
            "original": {
                "luid": orig.get("luid"),
                "name": orig["name"],
            },
            "new": {
                "luid": new_out.get("luid"),
                "name": new_out["name"],
                "source_flow_luid": df["publish"].get("flow_luid"),
                "source_flow_name": df["name"],
            },
        })
        pair_idx += 1

    return pairs, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--manifest", required=True,
                        help="Path to publish-manifest.json")
    parser.add_argument("--output", required=True,
                        help="Path to write pairs.json")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not manifest_path.is_file():
        sys.exit(f"ERROR: manifest not found: {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    pairs, warnings = build_pairs(manifest)

    payload = {
        "schema_version": "1",
        "generated_at": jst_now_iso(),
        "manifest_path": str(manifest_path).replace("\\", "/"),
        "original_flow_luid": manifest["original"].get("flow_luid"),
        "original_flow_name": manifest["original"].get("flow_name"),
        "pairs": pairs,
        "warnings": warnings,
    }

    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[resolve_pairs] Wrote {len(pairs)} pair(s) to {output_path}", file=sys.stderr)
    for w in warnings:
        print(f"[resolve_pairs] WARNING: {w}", file=sys.stderr)

    # If any LUID is missing, exit non-zero so the fork agent stops rather than
    # passing null LUIDs to MCP and getting cryptic errors downstream.
    missing_luid = (
        not payload["original_flow_luid"]
        or any(
            not p["original"]["luid"] or not p["new"]["luid"]
            for p in pairs
        )
    )
    if missing_luid:
        print(
            "[resolve_pairs] ERROR: one or more LUIDs are null in pairs.json. "
            "Run `python scripts/publish_manifest.py resolve-luids "
            f"--manifest {manifest_path}` and retry.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
