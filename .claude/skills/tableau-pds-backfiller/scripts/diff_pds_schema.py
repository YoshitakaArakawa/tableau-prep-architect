#!/usr/bin/env python3
"""Diff the column schema of two Published Data Sources for backfill safety.

Input = two JSON files in the shape returned by the Tableau MCP tool
`mcp__tableau__get-datasource-metadata` (fieldGroups[].fields[] with
name / dataType / role). No auth, no network -- a pure diff so the result is
reproducible and the raw metadata is kept as evidence.

Reconciliation intent: for an append-mode backfill (old accumulated PDS ->
new incremental accumulator PDS), the accumulator's live column NAMES and
TYPES must match the old PDS, or the append inserts NULLs / fails. This reports,
on the intersection and the symmetric difference:
  - columns only in OLD (would be lost on append)
  - columns only in NEW (would be NULL for backfilled rows)
  - name matches whose dataType differs (append coercion risk)
  - whether the incremental control field exists on both sides

Known renames (a column that was renamed during decomposition) are NOT drift:
pass `--rename OLD=NEW` (repeatable) to pre-map an old column name onto its new
name before diffing, so it is scored as a match, not as only_old + only_new.

Usage:
  python diff_pds_schema.py OLD.json NEW.json [--control Date]
  python diff_pds_schema.py OLD.json NEW.json --rename "Trade Date=Date" --json-out diff.json

Exit code 0 = clean match (append-safe on names/types), 1 = differences found.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_fields(path: Path) -> dict[str, dict]:
    """Return {name: {dataType, role}} flattened across all fieldGroups."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for group in doc.get("fieldGroups", []):
        for f in group.get("fields", []):
            # calculated fields / params can lack a physical column class; keep
            # only real columns since only those participate in an append.
            if f.get("columnClass") and f["columnClass"] != "COLUMN":
                continue
            out[f["name"]] = {
                "dataType": f.get("dataType"),
                "role": f.get("role"),
            }
    return out


def apply_renames(old: dict[str, dict], renames: dict[str, str]) -> dict[str, dict]:
    """Re-key OLD columns per `renames` (old_name -> new_name) so a decomposition
    rename lines up with the NEW schema instead of showing as drift."""
    if not renames:
        return old
    out = {}
    for name, meta in old.items():
        out[renames.get(name, name)] = meta
    return out


def parse_renames(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs or []:
        if "=" not in p:
            sys.exit(f"--rename expects OLD=NEW, got {p!r}")
        old_name, new_name = p.split("=", 1)
        out[old_name.strip()] = new_name.strip()
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("old", type=Path, help="OLD (source) PDS metadata JSON")
    ap.add_argument("new", type=Path, help="NEW (accumulator) PDS metadata JSON")
    ap.add_argument("--control", default=None,
                    help="incremental control field name (NEW-side) to assert on both")
    ap.add_argument("--rename", action="append", default=[],
                    help="OLD=NEW column rename to pre-map before diffing (repeatable)")
    ap.add_argument("--json-out", type=Path, help="write the diff result as JSON")
    args = ap.parse_args(argv)

    renames = parse_renames(args.rename)
    old = apply_renames(load_fields(args.old), renames)
    new = load_fields(args.new)

    only_old = sorted(set(old) - set(new))
    only_new = sorted(set(new) - set(old))
    common = sorted(set(old) & set(new))
    type_mismatch = [
        (n, old[n]["dataType"], new[n]["dataType"])
        for n in common
        if old[n]["dataType"] != new[n]["dataType"]
    ]

    print(f"OLD {args.old.name}: {len(old)} columns (after {len(renames)} rename(s))")
    print(f"NEW {args.new.name}: {len(new)} columns")
    print(f"common: {len(common)}  only_old: {len(only_old)}  "
          f"only_new: {len(only_new)}  type_mismatch: {len(type_mismatch)}")

    if only_old:
        print("\n[only in OLD -> lost on append]")
        for n in only_old:
            print(f"  - {n} ({old[n]['dataType']})")
    if only_new:
        print("\n[only in NEW -> NULL for backfilled rows]")
        for n in only_new:
            print(f"  - {n} ({new[n]['dataType']})")
    if type_mismatch:
        print("\n[type mismatch -> append coercion risk]")
        for n, o, w in type_mismatch:
            print(f"  - {n}: OLD={o} NEW={w}")

    control_ok = True
    control_report = None
    if args.control:
        in_old = args.control in old
        in_new = args.control in new
        control_ok = in_old and in_new
        print(f"\ncontrol field '{args.control}': OLD={in_old} NEW={in_new} "
              f"({'OK' if control_ok else 'MISSING'})")
        control_report = {"field": args.control, "in_old": in_old, "in_new": in_new}
        if control_ok:
            control_report["old_type"] = old[args.control]["dataType"]
            control_report["new_type"] = new[args.control]["dataType"]
            print(f"  OLD type={old[args.control]['dataType']} "
                  f"NEW type={new[args.control]['dataType']}")

    clean = not (only_old or only_new or type_mismatch) and control_ok
    print(f"\nRESULT: {'CLEAN - names+types match, append-safe' if clean else 'DIFFERENCES - reconcile before backfill'}")

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps({
            "clean": clean,
            "renames_applied": renames,
            "only_old": only_old,
            "only_new": only_new,
            "type_mismatch": [
                {"column": n, "old": o, "new": w} for n, o, w in type_mismatch],
            "control": control_report,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"diff JSON -> {args.json_out}")

    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
