#!/usr/bin/env python3
"""Read/write/update the session publish-manifest.json.

The manifest is a single JSON file at <work_session>/reports/publish-manifest.json
that aggregates: original flow info, decomposed flow list, output mapping,
publish/run status, and LUIDs as they become known.

Lifecycle:
  - tableau-prep-builder runs `init`
  - tableau-prep-deployer runs `update-publish` after each successful publish,
    `update-run` after each run, and `resolve-luids` once at the end of the
    chain to fill in original.flow_luid + all PDS LUIDs from Metadata API
  - tableau-pds-comparator reads it (no writes)

See references/publish-manifest-format.md for the full schema.

Commands:
  init             Build initial manifest from decomposition-plan + flow-summary + flows/
  update-publish   Mark a decomposed flow as published (or failed) and record LUID
  update-run       Record run finish_code for a decomposed flow
  resolve-luids    Query Metadata API to fill in remaining LUIDs
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "1"
NODE_TYPE_PUBLISH_EXTRACT = ".v1.PublishExtract"
LAYER_DIRS = ("staging", "intermediate", "marts")

# Map directory name -> manifest layer string. The two are identical here, but
# we keep the mapping explicit so renaming one doesn't silently break the other.
LAYER_DIR_TO_NAME = {d: d for d in LAYER_DIRS}


# ---------------------------------------------------------------------------
# Time / IO helpers
# ---------------------------------------------------------------------------

def jst_now_iso() -> str:
    """Return current time as ISO-8601 with JST (+09:00) offset, second precision."""
    return datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds")


def load_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_manifest(path: Path, data: dict[str, Any]) -> None:
    data["generated_at"] = jst_now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

_OUTPUT_MAPPING_HEADER = re.compile(
    r"^##\s+Output\s+mapping\s*\(.*?\)\s*$", re.MULTILINE
)
_NEXT_H2 = re.compile(r"^##\s+\S", re.MULTILINE)
_TABLE_ROW = re.compile(r"^\|(.+)\|\s*$")


def parse_output_mapping(plan_md: str) -> list[dict[str, str]]:
    """Extract the Output mapping table from decomposition-plan markdown.

    Returns a list of dicts: {original_output_pds, decomposed_flow_name,
    decomposed_output_pds}. The section header is matched case-sensitively
    against `## Output mapping (...)` and the first markdown table inside that
    section is parsed.

    Raises ValueError if the section or table is missing/malformed.
    """
    header_match = _OUTPUT_MAPPING_HEADER.search(plan_md)
    if not header_match:
        raise ValueError(
            "decomposition-plan is missing the `## Output mapping (...)` section. "
            "See references/decomposition-plan-format.md."
        )

    # Slice from the section start to the next H2 (or EOF).
    section_start = header_match.end()
    rest = plan_md[section_start:]
    next_h2 = _NEXT_H2.search(rest)
    section = rest[: next_h2.start()] if next_h2 else rest

    # Find table rows. A table row starts with `|`. Skip the header row and
    # separator row (the second row, all dashes).
    rows: list[list[str]] = []
    for line in section.splitlines():
        m = _TABLE_ROW.match(line)
        if not m:
            continue
        cells = [c.strip() for c in m.group(1).split("|")]
        rows.append(cells)

    if len(rows) < 3:
        raise ValueError(
            "Output mapping table requires a header, separator, and at least 1 data row."
        )

    header_row = rows[0]
    separator = rows[1]
    data_rows = rows[2:]

    if not all(set(cell) <= set("-: ") for cell in separator):
        raise ValueError("Output mapping table separator row is malformed.")

    if len(header_row) != 3:
        raise ValueError(
            f"Output mapping table must have 3 columns (got {len(header_row)}): "
            "Original output PDS | Decomposed flow | Decomposed output PDS"
        )

    out = []
    for r in data_rows:
        if len(r) != 3:
            raise ValueError(f"Output mapping table row has wrong column count: {r}")
        out.append({
            "original_output_pds": r[0],
            "decomposed_flow_name": r[1],
            "decomposed_output_pds": r[2],
        })
    return out


_FLOW_NAME_LINE = re.compile(r"^-\s+Flow name:\s*(.+?)\s*(?:\(|$)", re.MULTILINE)
_OUTPUTS_LINE = re.compile(
    r"^-\s+Outputs:\s+\d+\s+\((.+?)\)", re.MULTILINE
)


def parse_flow_summary(summary_md: str) -> dict[str, Any]:
    """Extract original flow name and output PDS names from flow-summary.md.

    The Meta section has lines like:
        - Flow name: stock-market-transaction-prep (derived from .tfl filename; ...)
        - Outputs: 2 (`stockmarket_transaction_prepped`, `stockmarket_transaction_detailed_prepped`) ...

    Returns {flow_name, outputs: [{name}, ...]}. Backtick wrapping on names is stripped.
    """
    name_m = _FLOW_NAME_LINE.search(summary_md)
    if not name_m:
        raise ValueError("flow-summary.md is missing a `- Flow name:` line.")
    flow_name = name_m.group(1).strip()

    outs_m = _OUTPUTS_LINE.search(summary_md)
    if not outs_m:
        raise ValueError("flow-summary.md is missing a `- Outputs: N (...)` line.")
    # outs_m.group(1) is the parenthesised list, e.g.
    #   `stockmarket_transaction_prepped`, `stockmarket_transaction_detailed_prepped`
    raw_names = [s.strip().strip("`") for s in outs_m.group(1).split(",")]
    outputs = [{"name": n, "luid": None} for n in raw_names if n]

    return {"flow_name": flow_name, "outputs": outputs}


def extract_publish_outputs_from_tfl(tfl_path: Path) -> list[dict[str, str | None]]:
    """Read a .tfl/.tflx and return its PublishExtract outputs.

    Returns a list of {name, luid: None} (one per PublishExtract node).
    A flow with only Hyper outputs returns an empty list.
    """
    with zipfile.ZipFile(tfl_path) as z:
        with z.open("flow") as f:
            flow = json.load(f)
    outs = []
    for node in (flow.get("nodes") or {}).values():
        if node.get("nodeType") == NODE_TYPE_PUBLISH_EXTRACT:
            name = node.get("datasourceName")
            if not name:
                continue
            outs.append({"name": name, "luid": None})
    return outs


# ---------------------------------------------------------------------------
# Manifest mutators
# ---------------------------------------------------------------------------

def find_decomposed(manifest: dict[str, Any], flow_name: str) -> dict[str, Any]:
    for df in manifest["decomposed_flows"]:
        if df["name"] == flow_name:
            return df
    raise KeyError(
        f"decomposed flow '{flow_name}' not found in manifest. "
        f"Known: {[df['name'] for df in manifest['decomposed_flows']]}"
    )


# ---------------------------------------------------------------------------
# Metadata API helpers (resolve-luids)
# ---------------------------------------------------------------------------

def _query_flows_outputs(server, flow_luids: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch downstreamDatasources for MANY flows in one Metadata API call.

    Returns {flow_luid: {luid, name, downstreamDatasources: [...]}}. One
    GraphQL round-trip regardless of flow count — resolve-luids previously
    issued one query per flow, which dominated its 14-42s wall time.
    """
    if not flow_luids:
        return {}
    query = """
    query FlowsDownstreamDatasources($luids: [String]) {
      flows(filter: { luidWithin: $luids }) {
        luid
        name
        downstreamDatasources { luid name }
      }
    }
    """
    result = server.metadata.query(query=query, variables={"luids": flow_luids})
    if "errors" in result and result["errors"]:
        msgs = "; ".join(e.get("message", "?") for e in result["errors"])
        raise RuntimeError(f"Metadata API error for flows {flow_luids}: {msgs}")
    flows = result.get("data", {}).get("flows", [])
    return {f["luid"]: f for f in flows}


def _fetch_all_flows(server) -> list[Any]:
    """All flows on the site, every page (flows.get() alone caps at one page)."""
    import tableauserverclient as TSC  # lazy: only resolve-luids needs it
    return list(TSC.Pager(server.flows))


def _find_flow_luid_by_name(all_flows: list[Any], flow_name: str) -> str | None:
    """Resolve a flow LUID by exact name from a pre-fetched flow list.

    Returns None if not found, or the LUID if exactly one match. Raises on
    ambiguous match.
    """
    matches = [f for f in all_flows if f.name == flow_name]
    if not matches:
        return None
    if len(matches) > 1:
        raise RuntimeError(
            f"Multiple flows named '{flow_name}' found "
            f"({[(f.id, f.project_name) for f in matches]}); "
            "set original.flow_luid manually to disambiguate."
        )
    return matches[0].id


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    flows_dir = Path(args.flows_dir)
    output_path = Path(args.output)

    if output_path.exists() and not args.force:
        print(
            f"[publish_manifest] ERROR: {output_path} already exists. "
            f"Re-init would overwrite publish/run state from prior runs. "
            f"Pass --force to overwrite, or omit init to preserve the existing manifest.",
            file=sys.stderr,
        )
        return 1

    provisioning_entries: list[dict[str, Any]] = []
    if args.plan_json:
        # plan.json is the single source of truth: original flow identity,
        # output mapping, AND needs_provisioning stg entries come from it —
        # no markdown parsing (the md is a rendered view of the same plan).
        plan = json.loads(Path(args.plan_json).read_text(encoding="utf-8"))
        source_by_flow = {
            f["name"]: f["source_original_output_name"]
            for f in plan.get("flows", [])
            if f.get("source_original_output_name")
        }
        original = {
            "flow_name": plan["flow_name"],
            "outputs": [
                {"name": o["name"], "luid": o.get("luid")}
                for o in (plan.get("original") or {}).get("outputs", [])
            ],
        }
        if args.original_flow_luid is None:
            args.original_flow_luid = (plan.get("original") or {}).get("flow_luid")
        mapping_rows = [
            {"decomposed_flow_name": k, "original_output_pds": v}
            for k, v in source_by_flow.items()
        ]
        for f in plan.get("flows", []):
            if f.get("input_status") != "needs_provisioning":
                continue
            provisioning_entries.append({
                "name": f["name"],
                "layer": f["layer"],
                "kind": "tfl",
                "tfl_path": None,
                "source_original_output_name": f.get("source_original_output_name"),
                "publish": {"status": "skipped_pending_provisioning",
                            "flow_luid": None, "published_at": None},
                "run": {"status": "n/a", "finish_code": None, "run_at": None},
                "outputs": [{"name": (f.get("output") or {}).get("name") or f["name"],
                             "luid": None}],
            })
    else:
        plan_md = Path(args.decomposition_plan).read_text(encoding="utf-8")
        summary_md = Path(args.flow_summary).read_text(encoding="utf-8")

        mapping_rows = parse_output_mapping(plan_md)
        # Build a lookup: decomposed_flow_name -> source_original_output_name
        source_by_flow = {
            row["decomposed_flow_name"]: row["original_output_pds"]
            for row in mapping_rows
        }

        original = parse_flow_summary(summary_md)

    # Scan flows/{layer}/ for both .tfl (kind=tfl) and *.augmenter.json (kind=pds_augment).
    decomposed: list[dict[str, Any]] = []
    for layer_dir in LAYER_DIRS:
        ldir = flows_dir / layer_dir
        if not ldir.is_dir():
            continue
        for tfl in sorted(ldir.glob("*.tfl")):
            name = tfl.stem
            outs = extract_publish_outputs_from_tfl(tfl)
            decomposed.append({
                "name": name,
                "layer": LAYER_DIR_TO_NAME[layer_dir],
                "kind": "tfl",
                "tfl_path": str(tfl.relative_to(flows_dir.parent)).replace("\\", "/"),
                "source_original_output_name": source_by_flow.get(name),
                "publish": {"status": "pending", "flow_luid": None, "published_at": None},
                "run": {"status": "pending", "finish_code": None, "run_at": None},
                "outputs": outs,
            })
        for spec_path in sorted(ldir.glob("*.augmenter.json")):
            # name strips both .json and .augmenter suffixes to match the PDS name
            name = spec_path.stem
            if name.endswith(".augmenter"):
                name = name[: -len(".augmenter")]
            try:
                spec = json.loads(spec_path.read_text(encoding="utf-8"))
                target_name = (spec.get("target") or {}).get("new_name") or name
            except (json.JSONDecodeError, OSError):
                target_name = name
            decomposed.append({
                "name": name,
                "layer": LAYER_DIR_TO_NAME[layer_dir],
                "kind": "pds_augment",
                "augmenter_spec_path": str(spec_path.relative_to(flows_dir.parent)).replace("\\", "/"),
                "source_original_output_name": source_by_flow.get(name),
                "publish": {"status": "pending", "pds_luid": None, "published_at": None},
                # Live PDS has no materialize phase; run is recorded as n/a so the
                # deployer can skip it without flagging a missing run.
                "run": {"status": "n/a", "finish_code": None, "run_at": None},
                "outputs": [{"name": target_name, "luid": None}],
            })

    decomposed.extend(provisioning_entries)

    if not decomposed:
        print(
            f"[publish_manifest] WARNING: no .tfl files found under {flows_dir}",
            file=sys.stderr,
        )

    # Cross-check: every flow named in the Output mapping table must exist in flows_dir.
    found_names = {df["name"] for df in decomposed}
    missing = [r["decomposed_flow_name"] for r in mapping_rows if r["decomposed_flow_name"] not in found_names]
    if missing:
        print(
            f"[publish_manifest] WARNING: Output mapping references flows not found "
            f"under {flows_dir}: {missing}",
            file=sys.stderr,
        )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": None,  # filled by save_manifest
        "session_work_dir": str(flows_dir.parent).replace("\\", "/"),
        "original": {
            "flow_name": original["flow_name"],
            "flow_luid": args.original_flow_luid,
            "outputs": original["outputs"],
        },
        "decomposed_flows": decomposed,
    }
    save_manifest(output_path, manifest)
    print(
        f"[publish_manifest] init: wrote {len(decomposed)} decomposed flow(s) "
        f"to {output_path}",
        file=sys.stderr,
    )
    return 0


def cmd_update_publish(args: argparse.Namespace) -> int:
    path = Path(args.manifest)
    m = load_manifest(path)
    df = find_decomposed(m, args.flow_name)
    kind = df.get("kind", "tfl")
    df["publish"]["status"] = args.status
    if args.status == "published":
        if kind == "pds_augment":
            # PDS LUID lives under 'pds_luid' for augment entries (no flow_luid).
            if not args.pds_luid:
                print(
                    f"[publish_manifest] ERROR: --pds-luid is required when updating "
                    f"a kind=pds_augment entry ({args.flow_name})",
                    file=sys.stderr,
                )
                return 1
            df["publish"]["pds_luid"] = args.pds_luid
            df["publish"]["published_at"] = jst_now_iso()
            # outputs[0].luid mirrors pds_luid for direct lookup by comparator.
            if df.get("outputs"):
                df["outputs"][0]["luid"] = args.pds_luid
        else:
            df["publish"]["flow_luid"] = args.flow_luid
            df["publish"]["published_at"] = jst_now_iso()
    save_manifest(path, m)
    luid_field = "pds_luid" if kind == "pds_augment" else "flow_luid"
    luid_val = args.pds_luid if kind == "pds_augment" else args.flow_luid
    print(
        f"[publish_manifest] update-publish: {args.flow_name} (kind={kind}) -> "
        f"status={args.status}, {luid_field}={luid_val}",
        file=sys.stderr,
    )
    return 0


def cmd_update_run(args: argparse.Namespace) -> int:
    path = Path(args.manifest)
    m = load_manifest(path)
    df = find_decomposed(m, args.flow_name)
    if df.get("kind") == "pds_augment":
        # Live PDS entries have no run phase; reject silently-fatal misuse.
        print(
            f"[publish_manifest] ERROR: update-run not applicable to "
            f"kind=pds_augment entry ({args.flow_name}); Live PDS has no run phase",
            file=sys.stderr,
        )
        return 1
    fc = args.finish_code
    status = {0: "success", 1: "failed", 2: "failed"}.get(fc, "failed")
    df["run"]["status"] = status
    df["run"]["finish_code"] = fc
    df["run"]["run_at"] = jst_now_iso()
    save_manifest(path, m)
    print(
        f"[publish_manifest] update-run: {args.flow_name} -> "
        f"status={status}, finish_code={fc}",
        file=sys.stderr,
    )
    return 0


def cmd_resolve_luids(args: argparse.Namespace) -> int:
    from tableau_auth import signed_in_server  # noqa: E402

    path = Path(args.manifest)
    m = load_manifest(path)

    with signed_in_server() as server:
        # 1. Name -> LUID resolution: ONE full flow listing for every entry
        #    that still lacks a flow_luid (originally one REST scan per entry).
        needs_name_lookup = (
            not m["original"]["flow_luid"]
            or any(df.get("kind") != "pds_augment" and not df["publish"].get("flow_luid")
                   for df in m["decomposed_flows"])
        )
        all_flows = _fetch_all_flows(server) if needs_name_lookup else []

        if not m["original"]["flow_luid"]:
            luid = _find_flow_luid_by_name(all_flows, m["original"]["flow_name"])
            if luid is None:
                print(
                    f"[publish_manifest] WARNING: original flow "
                    f"'{m['original']['flow_name']}' not found on server",
                    file=sys.stderr,
                )
            m["original"]["flow_luid"] = luid

        for df in m["decomposed_flows"]:
            if df.get("kind") == "pds_augment":
                # Live PDS entries have no flow to resolve; pds_luid was set at
                # publish time. Still backfill outputs[0].luid from pds_luid in
                # case an older entry pre-dates that mirror.
                pds_luid = df["publish"].get("pds_luid")
                if pds_luid and df.get("outputs") and not df["outputs"][0].get("luid"):
                    df["outputs"][0]["luid"] = pds_luid
                continue
            if not df["publish"].get("flow_luid"):
                luid = _find_flow_luid_by_name(all_flows, df["name"])
                if luid is None:
                    continue
                df["publish"]["flow_luid"] = luid
                if df["publish"]["status"] == "pending":
                    df["publish"]["status"] = "published"

        # 2. Output PDS LUIDs: ONE Metadata API query covering the original
        #    flow and every decomposed flow (originally one query per flow).
        want_luids = []
        if m["original"]["flow_luid"]:
            want_luids.append(m["original"]["flow_luid"])
        want_luids += [
            df["publish"]["flow_luid"] for df in m["decomposed_flows"]
            if df.get("kind") != "pds_augment" and df["publish"].get("flow_luid")
            and df.get("outputs")
        ]
        outputs_by_flow = _query_flows_outputs(server, want_luids)

        def backfill(flow_luid: str | None, outputs: list[dict[str, Any]]) -> None:
            info = outputs_by_flow.get(flow_luid or "")
            if not info:
                return
            by_name = {d["name"]: d["luid"]
                       for d in info.get("downstreamDatasources") or []}
            for o in outputs:
                if not o.get("luid"):
                    o["luid"] = by_name.get(o["name"])

        backfill(m["original"]["flow_luid"], m["original"]["outputs"])
        for df in m["decomposed_flows"]:
            if df.get("kind") != "pds_augment":
                backfill(df["publish"].get("flow_luid"), df.get("outputs") or [])

    save_manifest(path, m)
    print(f"[publish_manifest] resolve-luids: updated {path}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read/write the session publish-manifest.json"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Build initial manifest after build")
    p_init.add_argument("--plan-json", default=None,
                        help="Path to decomposition-plan-<flow>.json. Preferred: "
                             "replaces --decomposition-plan/--flow-summary parsing")
    p_init.add_argument("--decomposition-plan", default=None,
                        help="Path to decomposition-plan-<flow>.md "
                             "(legacy path; superseded by --plan-json)")
    p_init.add_argument("--flow-summary", default=None,
                        help="Path to flow-summary.md (legacy path; requires "
                             "an `- Outputs: N (...)` Meta line)")
    p_init.add_argument("--flows-dir", required=True,
                        help="Path to flows/ directory containing staging/, intermediate/, marts/")
    p_init.add_argument("--output", required=True,
                        help="Path to write publish-manifest.json")
    p_init.add_argument("--original-flow-luid", default=None,
                        help="Original flow LUID if known (from session intake Q1)")
    p_init.add_argument("--force", action="store_true",
                        help="Overwrite an existing manifest at --output. "
                             "Default behaviour errors out to preserve prior publish/run state.")
    p_init.set_defaults(func=cmd_init)

    p_up = sub.add_parser("update-publish", help="Mark a decomposed flow as published or failed")
    p_up.add_argument("--manifest", required=True, help="Path to publish-manifest.json")
    p_up.add_argument("--flow-name", required=True, help="Decomposed flow name (.tfl stem or augmenter spec stem)")
    p_up.add_argument("--status", required=True, choices=["published", "failed"],
                      help="Publish outcome")
    p_up.add_argument("--flow-luid", default=None,
                      help="Required when --status=published and the entry is kind=tfl")
    p_up.add_argument("--pds-luid", default=None,
                      help="Required when --status=published and the entry is kind=pds_augment")
    p_up.set_defaults(func=cmd_update_publish)

    p_ur = sub.add_parser("update-run", help="Record run finish_code for a flow")
    p_ur.add_argument("--manifest", required=True, help="Path to publish-manifest.json")
    p_ur.add_argument("--flow-name", required=True)
    p_ur.add_argument("--finish-code", required=True, type=int, choices=[0, 1, 2])
    p_ur.set_defaults(func=cmd_update_run)

    p_rl = sub.add_parser("resolve-luids",
                          help="Fill in original.flow_luid + all PDS LUIDs from Metadata API")
    p_rl.add_argument("--manifest", required=True)
    p_rl.set_defaults(func=cmd_resolve_luids)

    args = parser.parse_args()

    if args.cmd == "init":
        if not args.plan_json and not (args.decomposition_plan and args.flow_summary):
            parser.error(
                "init requires --plan-json, or both --decomposition-plan and "
                "--flow-summary (legacy)"
            )

    if args.cmd == "update-publish" and args.status == "published":
        # Exactly one of --flow-luid / --pds-luid is required; per-entry kind
        # is checked inside cmd_update_publish for the right one.
        if not args.flow_luid and not args.pds_luid:
            parser.error(
                "--flow-luid (kind=tfl) or --pds-luid (kind=pds_augment) is "
                "required when --status=published"
            )

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
