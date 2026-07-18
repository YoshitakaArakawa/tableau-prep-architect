#!/usr/bin/env python3
"""Backfill historical rows from an old accumulated PDS into a new incremental
accumulator PDS by hyper-level surgery, then (with --execute) republish Overwrite.

Mechanism (per flow entry in the spec):
  1. download old + new PDS as .tdsx (include_extract=True)
  2. copy the downloaded NEW .tdsx to snapshot/ as the ROLLBACK point (restore
     with --restore <tag> if the Overwrite goes wrong)
  3. open both .hyper extracts on ONE connection (attach_database), then either:
       seam mode (default): INSERT the OLD rows whose control < MIN(new.control)
         into NEW -- strictly older than the new baseline, so no overlap, no
         duplicate; NEW stays authoritative for [seam, new_max]. Watermark
         (MAX control) is unchanged, so the next incremental run still appends
         only source rows past the existing max.
       replace mode: DELETE NEW, then load ALL old rows -- for a sentinel/
         placeholder baseline whose lone control value sits below all real
         history (seam rule would insert 0). Watermark becomes old_max.
     Columns are aligned BY NAME (order-independent). Renamed / cast columns are
     bridged via the entry's column_map. The seam comparison runs inside Hyper
     SQL (INSERT ... SELECT ... WHERE control < (SELECT MIN(control) FROM new)),
     so rows never round-trip through Python (scales to large extracts) and the
     control value is never re-formatted as a literal (timezone-faithful).
  4. verify the local NEW row count == expected, rezip -> <tag>_backfilled.tdsx
  5. --execute only: publish Overwrite to the same PDS (LUID/content_url kept),
     then re-download and verify server row count + MAX(control).

Every run appends to backfill-manifest.json (audit: what / seam / before-after
counts / snapshot path / when). NEVER full-run the accumulator after this -- a
full run of an append output re-appends the whole snapshot and duplicates.

Default is DRY RUN (local only, no publish). Pass --execute to publish.

Usage:
  python backfill_pds.py --spec backfill-spec.json                  # dry-run all
  python backfill_pds.py --spec backfill-spec.json --only f02       # dry-run one
  python backfill_pds.py --spec backfill-spec.json --only f02 --execute
  python backfill_pds.py --spec backfill-spec.json --restore f02    # roll back one
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "scripts"))

import tableauserverclient as TSC  # noqa: E402

from tableau_auth import signed_in_server  # noqa: E402

from tableauhyperapi import (  # noqa: E402
    Connection,
    DatabaseName,
    HyperProcess,
    Telemetry,
)


def escape_name(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def unzip_tdsx(tdsx: Path, dest: Path) -> Path:
    """Extract .tdsx, return the single .hyper path inside."""
    with zipfile.ZipFile(tdsx) as z:
        z.extractall(dest)
    hypers = list(dest.rglob("*.hyper"))
    if len(hypers) != 1:
        raise RuntimeError(f"expected exactly 1 .hyper in {tdsx.name}, found {len(hypers)}")
    return hypers[0]


def rezip_tdsx(src_dir: Path, out_tdsx: Path) -> None:
    """Repackage an unzipped .tdsx directory back into a .tdsx (zip)."""
    if out_tdsx.exists():
        out_tdsx.unlink()
    with zipfile.ZipFile(out_tdsx, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(src_dir.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(src_dir).as_posix())


def sole_table(conn: Connection, db_alias: str):
    """Return the single user TableName in one attached database."""
    tables = []
    for schema in conn.catalog.get_schema_names(DatabaseName(db_alias)):
        tables.extend(conn.catalog.get_table_names(schema))
    if len(tables) != 1:
        raise RuntimeError(f"expected exactly 1 table in {db_alias!r}, found {tables}")
    return tables[0]


def col_names(table_def) -> list[str]:
    return [c.name.unescaped for c in table_def.columns]


def build_column_map(cfg: dict) -> dict[str, tuple[str, str | None]]:
    """new_name -> (old_name, cast_type_or_None). Identity when unmapped."""
    mp: dict[str, tuple[str, str | None]] = {}
    for m in cfg.get("column_map", []):
        mp[m["new"]] = (m["old"], m.get("cast"))
    return mp


def build_select(new_cols: list[str], old_cols: set[str], mp: dict, tag: str
                 ) -> tuple[list[str], list[str]]:
    """Return (insert_col_list, select_expr_list), aligned by NAME. Escalates if
    a NEW column has no OLD counterpart (identity or column_map)."""
    insert_cols, select_exprs = [], []
    for nc in new_cols:
        oc, cast = mp.get(nc, (nc, None))
        if oc not in old_cols:
            raise RuntimeError(
                f"[{tag}] NEW column {nc!r} maps to OLD column {oc!r}, absent in "
                f"OLD extract. Add a column_map entry or escalate (schema not "
                f"reconcilable by rename/cast).")
        insert_cols.append(escape_name(nc))
        expr = escape_name(oc)
        if cast:
            expr = f"CAST({expr} AS {cast})"
        select_exprs.append(expr)
    return insert_cols, select_exprs


def backfill_one(server, hyper, tag: str, cfg: dict, workdir: Path,
                 execute: bool) -> dict:
    new_ctrl = cfg["control"]
    mode = cfg.get("mode", "seam")
    mp = build_column_map(cfg)
    old_ctrl = mp.get(new_ctrl, (new_ctrl, None))[0]

    dl = workdir / "download" / tag
    (dl / "old").mkdir(parents=True, exist_ok=True)
    (dl / "new").mkdir(parents=True, exist_ok=True)
    snap_dir = workdir / "snapshot"
    snap_dir.mkdir(parents=True, exist_ok=True)

    old_tdsx = Path(server.datasources.download(
        cfg["old_luid"], filepath=str(dl / "old"), include_extract=True))
    new_tdsx = Path(server.datasources.download(
        cfg["new_luid"], filepath=str(dl / "new"), include_extract=True))
    print(f"[{tag}] downloaded old={old_tdsx.name} new={new_tdsx.name}")

    # Snapshot the NEW .tdsx BEFORE any local mutation -> rollback point.
    snapshot = snap_dir / f"{tag}_new_pre_backfill.tdsx"
    shutil.copy2(new_tdsx, snapshot)
    print(f"[{tag}] snapshot -> {snapshot.name}")

    old_hyper = unzip_tdsx(old_tdsx, dl / "old_x")
    new_dir = dl / "new_x"
    new_hyper = unzip_tdsx(new_tdsx, new_dir)

    with Connection(hyper.endpoint) as conn:
        conn.catalog.attach_database(str(new_hyper), "newdb")
        conn.catalog.attach_database(str(old_hyper), "olddb")
        new_tn = sole_table(conn, "newdb")
        old_tn = sole_table(conn, "olddb")
        new_tbl, old_tbl = str(new_tn), str(old_tn)
        new_def = conn.catalog.get_table_definition(new_tn)
        old_def = conn.catalog.get_table_definition(old_tn)
        new_cols = col_names(new_def)
        old_cols = set(col_names(old_def))

        insert_cols, select_exprs = build_select(new_cols, old_cols, mp, tag)

        nc, oc = escape_name(new_ctrl), escape_name(old_ctrl)
        new_count = conn.execute_scalar_query(f"SELECT COUNT(*) FROM {new_tbl}")
        seam = conn.execute_scalar_query(f"SELECT MIN({nc}) FROM {new_tbl}")
        new_max = conn.execute_scalar_query(f"SELECT MAX({nc}) FROM {new_tbl}")
        new_distinct = conn.execute_scalar_query(
            f"SELECT COUNT(DISTINCT {nc}) FROM {new_tbl}")
        old_count = conn.execute_scalar_query(f"SELECT COUNT(*) FROM {old_tbl}")
        old_min = conn.execute_scalar_query(f"SELECT MIN({oc}) FROM {old_tbl}")
        old_max = conn.execute_scalar_query(f"SELECT MAX({oc}) FROM {old_tbl}")

        insert_list = ", ".join(insert_cols)
        select_list = ", ".join(select_exprs)
        if mode == "replace":
            to_insert = old_count
            expected_new_total = old_count
            conn.execute_command(f"DELETE FROM {new_tbl}")
            conn.execute_command(
                f"INSERT INTO {new_tbl} ({insert_list}) "
                f"SELECT {select_list} FROM {old_tbl}")
        else:  # seam
            to_insert = conn.execute_scalar_query(
                f"SELECT COUNT(*) FROM {old_tbl} "
                f"WHERE {oc} IS NOT NULL AND {oc} < (SELECT MIN({nc}) FROM {new_tbl})")
            expected_new_total = new_count + to_insert
            conn.execute_command(
                f"INSERT INTO {new_tbl} ({insert_list}) "
                f"SELECT {select_list} FROM {old_tbl} "
                f"WHERE {oc} IS NOT NULL AND {oc} < (SELECT MIN({nc}) FROM {new_tbl})")

        after = conn.execute_scalar_query(f"SELECT COUNT(*) FROM {new_tbl}")
        local_max = conn.execute_scalar_query(f"SELECT MAX({nc}) FROM {new_tbl}")

    ok = after == expected_new_total
    # Sentinel heuristic: the new baseline's WHOLE control range sits at/below all
    # old history (new_max <= old_min), so the seam rule inserts 0 -> the baseline
    # is probably a placeholder and replace mode is meant. Using new_max (not seam)
    # avoids a false positive when NEW already holds full history down to old_min.
    sentinel_warning = (
        mode == "seam" and to_insert == 0 and old_count > 0
        and new_max is not None and old_min is not None and new_max <= old_min)

    report = {
        "tag": tag, "mode": mode, "control": new_ctrl,
        "old_luid": cfg["old_luid"], "new_luid": cfg["new_luid"],
        "old_count": old_count, "old_min": str(old_min), "old_max": str(old_max),
        "new_count_before": new_count, "new_min": str(seam), "new_max": str(new_max),
        "new_distinct_control": new_distinct,
        "seam": str(seam), "to_insert": to_insert,
        "expected_new_total": expected_new_total,
        "local_new_total_after": after, "local_max_control_after": str(local_max),
        "local_verify": "OK" if ok else "MISMATCH",
        "sentinel_warning": bool(sentinel_warning),
        "snapshot_tdsx": snapshot.relative_to(workdir).as_posix(),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }
    print(f"[{tag}] mode={mode}  old={old_count} ({old_min}..{old_max})  "
          f"new={new_count} ({seam}..{new_max})  seam={seam}")
    print(f"[{tag}] to_insert={to_insert}  expected_new_total={expected_new_total}  "
          f"after={after} ({'OK' if ok else 'MISMATCH'})")
    if sentinel_warning:
        print(f"[{tag}] WARNING: seam <= old_min and to_insert=0 -> new baseline "
              f"looks like a sentinel. Use mode='replace' for this entry.")

    backfilled = workdir / f"{tag}_backfilled.tdsx"
    rezip_tdsx(new_dir, backfilled)
    report["backfilled_tdsx"] = backfilled.relative_to(workdir).as_posix()
    print(f"[{tag}] wrote {backfilled.name} ({backfilled.stat().st_size} B)")

    if execute:
        if not ok:
            raise RuntimeError(f"[{tag}] refusing to publish: local verify MISMATCH")
        existing = server.datasources.get_by_id(cfg["new_luid"])
        ds_item = TSC.DatasourceItem(project_id=existing.project_id, name=existing.name)
        pub = server.datasources.publish(
            ds_item, str(backfilled), mode=TSC.Server.PublishMode.Overwrite)
        report["published_luid"] = pub.id
        report["executed"] = True
        print(f"[{tag}] PUBLISHED Overwrite luid={pub.id} name={pub.name}")
        report["server_verify"] = verify_server(server, hyper, cfg, workdir, tag,
                                                 new_ctrl, expected_new_total, mode,
                                                 str(old_max), str(new_max))
    else:
        report["executed"] = False
        report["published_luid"] = None
        report["server_verify"] = None
        print(f"[{tag}] DRY RUN -- not published")

    return report


def verify_server(server, hyper, cfg, workdir, tag, ctrl, expected_total,
                  mode, old_max, new_max_before) -> dict:
    """Re-download the just-published PDS and confirm row count + MAX(control)."""
    vdir = workdir / "download" / tag / "verify"
    vdir.mkdir(parents=True, exist_ok=True)
    vtdsx = Path(server.datasources.download(
        cfg["new_luid"], filepath=str(vdir), include_extract=True))
    vhyper = unzip_tdsx(vtdsx, vdir / "x")
    with Connection(hyper.endpoint) as conn:
        conn.catalog.attach_database(str(vhyper), "vdb")
        tbl = str(sole_table(conn, "vdb"))
        count = conn.execute_scalar_query(f"SELECT COUNT(*) FROM {tbl}")
        smax = conn.execute_scalar_query(
            f"SELECT MAX({escape_name(ctrl)}) FROM {tbl}")
    # seam mode preserves the pre-existing high-water mark; replace adopts old_max.
    expected_max = old_max if mode == "replace" else new_max_before
    result = {
        "server_count": count,
        "expected_count": expected_total,
        "count_ok": count == expected_total,
        "server_max_control": str(smax),
        "expected_max_control": expected_max,
        "max_ok": str(smax) == expected_max,
    }
    verdict = "OK" if result["count_ok"] and result["max_ok"] else "MISMATCH"
    print(f"[{tag}] server verify: count={count}/{expected_total} "
          f"max={smax} ({verdict})")
    result["verdict"] = verdict
    return result


def restore_one(server, tag: str, manifest: dict, workdir: Path) -> dict:
    """Republish the pre-backfill snapshot for one tag (rollback)."""
    entry = next((e for e in reversed(manifest.get("entries", []))
                  if e["tag"] == tag and e.get("snapshot_tdsx")), None)
    if entry is None:
        raise RuntimeError(f"no manifest entry with a snapshot for tag {tag!r}")
    snap = workdir / entry["snapshot_tdsx"]
    if not snap.exists():
        raise RuntimeError(f"snapshot missing on disk: {snap}")
    existing = server.datasources.get_by_id(entry["new_luid"])
    ds_item = TSC.DatasourceItem(project_id=existing.project_id, name=existing.name)
    pub = server.datasources.publish(
        ds_item, str(snap), mode=TSC.Server.PublishMode.Overwrite)
    print(f"[{tag}] RESTORED from {snap.name} luid={pub.id}")
    return {"tag": tag, "restored_from": entry["snapshot_tdsx"],
            "published_luid": pub.id,
            "timestamp": datetime.now().isoformat(timespec="seconds")}


def load_manifest(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"schema_version": "1", "entries": []}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--spec", required=True, help="backfill spec JSON")
    ap.add_argument("--only", help="run one entry by tag")
    ap.add_argument("--execute", action="store_true",
                    help="publish Overwrite (default: dry-run, local only)")
    ap.add_argument("--restore", metavar="TAG",
                    help="roll back one tag by republishing its snapshot")
    ap.add_argument("--workdir", help="output dir (default: spec dir / backfill_out)")
    args = ap.parse_args(argv)

    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    entries = {e["tag"]: e for e in spec["flows"]}
    workdir = Path(args.workdir) if args.workdir else \
        Path(args.spec).resolve().parent / "backfill_out"
    workdir.mkdir(parents=True, exist_ok=True)
    manifest_path = workdir / "backfill-manifest.json"
    manifest = load_manifest(manifest_path)

    if args.restore:
        with signed_in_server() as server:
            rec = restore_one(server, args.restore, manifest, workdir)
        manifest.setdefault("restores", []).append(rec)
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                                 encoding="utf-8")
        print("RESULT_JSON: " + json.dumps({"status": "restored", **rec},
                                            ensure_ascii=False))
        return 0

    tags = [args.only] if args.only else list(entries)
    for t in tags:
        if t not in entries:
            sys.exit(f"unknown tag {t!r}; choices: {list(entries)}")

    reports = []
    with HyperProcess(Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hyper:
        with signed_in_server() as server:
            for t in tags:
                print(f"\n===== {t} ({'EXECUTE' if args.execute else 'DRY RUN'}) =====")
                rep = backfill_one(server, hyper, t, entries[t], workdir, args.execute)
                reports.append(rep)
                manifest["entries"].append(rep)

    manifest["generated_at"] = datetime.now().isoformat(timespec="seconds")
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                             encoding="utf-8")

    out = workdir / ("report_execute.json" if args.execute else "report_dryrun.json")
    out.write_text(json.dumps(reports, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nreport -> {out}\nmanifest -> {manifest_path}")

    mismatches = [r["tag"] for r in reports if r["local_verify"] != "OK"]
    sentinels = [r["tag"] for r in reports if r.get("sentinel_warning")]
    srv_mismatch = [r["tag"] for r in reports
                    if (r.get("server_verify") or {}).get("verdict") == "MISMATCH"]
    print("RESULT_JSON: " + json.dumps({
        "status": "ok" if not (mismatches or srv_mismatch) else "verify_failed",
        "executed": args.execute,
        "entries": len(reports),
        "local_mismatches": mismatches,
        "server_mismatches": srv_mismatch,
        "sentinel_warnings": sentinels,
        "manifest": str(manifest_path),
    }, ensure_ascii=False))
    return 0 if not (mismatches or srv_mismatch) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
