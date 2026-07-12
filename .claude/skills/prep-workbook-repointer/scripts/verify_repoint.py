#!/usr/bin/env python3
"""Verify workbook repoint by re-scanning lineage. Read-only, no self-fix.

verify mode. Runs AFTER a human has replaced data sources in Tableau Desktop and
republished. For every (old PDS -> new PDS, workbook) triple in repoint-design.json
it re-reads the SAME confirmed lineage field used at design time
(`publishedDatasources { downstreamWorkbooks }`) and checks both directions of
the move using only forward queries:

  - old-side:  the workbook has DISAPPEARED from the old PDS downstreamWorkbooks
  - new-side:  the workbook has APPEARED in the new PDS downstreamWorkbooks

The reverse `upstreamDatasources` field is deliberately not used (unconfirmed;
assuming it risks empty-response false FAILs). Per-workbook verdict:

  - reflected      : old-removed AND new-present
  - partial        : exactly one side moved
  - not_reflected  : neither side moved

Metadata lineage updates lag republish (eventual consistency), so a fresh verify
can show not_reflected purely from lag. This tool takes a SINGLE snapshot and
ADVISES re-running later; it never edits anything and never "fixes" a FAIL — the
human re-runs verify, or re-does the Desktop step if it is genuinely missing.

Usage:
    python verify_repoint.py --design <repoint-design.json> --out <output_dir>/repoint-verify-report.md

Cloud access is read-only. Final line: RESULT_JSON: {...}
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))

from tableau_auth import signed_in_server  # noqa: E402


def jst_now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds")


# Same lineage query as inventory_workbooks.py (kept in sync deliberately — verify
# must observe through the exact field design observed, so a field quirk can't make
# one side see workbooks the other cannot).
LINEAGE_QUERY = """
{
  publishedDatasources {
    luid
    name
    downstreamWorkbooks { luid name }
  }
}
"""


def query_downstream_by_pds(server: Any) -> tuple[dict[str, set[str]], list[str]]:
    """Return ({pds_luid: {wb_luid,...}}, errors) from a single lineage read."""
    result = server.metadata.query(query=LINEAGE_QUERY)
    errors = [e.get("message", "?") for e in (result.get("errors") or [])]
    out: dict[str, set[str]] = {}
    for p in (result.get("data") or {}).get("publishedDatasources") or []:
        luid = p.get("luid")
        if not luid:
            continue
        out[luid] = {
            wb.get("luid")
            for wb in (p.get("downstreamWorkbooks") or [])
            if wb.get("luid")
        }
    return out, errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--design", required=True, help="repoint-design.json from design mode")
    ap.add_argument("--out", required=True, help="Write repoint-verify-report.md here")
    args = ap.parse_args()

    t0 = time.monotonic()
    design = json.loads(Path(args.design).read_text(encoding="utf-8"))
    pairs = design.get("pairs") or []

    with signed_in_server() as server:
        downstream, meta_errors = query_downstream_by_pds(server)
    t_query = time.monotonic()

    results: list[dict] = []
    for pr in pairs:
        old_luid = pr["old_pds"]["luid"]
        new_luid = pr["new_pds"]["luid"]
        old_set = downstream.get(old_luid, set())
        new_set = downstream.get(new_luid, set())
        old_seen = old_luid in downstream
        new_seen = new_luid in downstream
        for wb in pr["workbooks"]:
            wb_luid = wb["luid"]
            old_removed = wb_luid not in old_set
            new_present = wb_luid in new_set
            if old_removed and new_present:
                verdict = "reflected"
            elif old_removed or new_present:
                verdict = "partial"
            else:
                verdict = "not_reflected"
            results.append({
                "workbook": wb["name"],
                "workbook_luid": wb_luid,
                "old_pds": pr["old_pds"]["name"],
                "new_pds": pr["new_pds"]["name"],
                "old_removed": old_removed,
                "new_present": new_present,
                "verdict": verdict,
                "old_pds_seen": old_seen,
                "new_pds_seen": new_seen,
            })

    n = len(results)
    reflected = sum(1 for r in results if r["verdict"] == "reflected")
    partial = sum(1 for r in results if r["verdict"] == "partial")
    not_reflected = sum(1 for r in results if r["verdict"] == "not_reflected")
    # PASS only when every checked workbook is fully reflected. Otherwise
    # INCOMPLETE — could be lag or a genuinely missed Desktop step; the report
    # explains and advises re-running rather than asserting a hard failure.
    overall = "PASS" if n and reflected == n else ("INCOMPLETE" if n else "EMPTY")

    lines: list[str] = []
    lines.append("# Workbook Repoint Verify Report")
    lines.append("")
    lines.append(f"- Generated at: {jst_now_iso()}")
    lines.append(f"- Design: {args.design}".replace("\\", "/"))
    lines.append(f"- **Overall verdict: {overall}** "
                 f"({reflected} reflected / {partial} partial / {not_reflected} not_reflected / {n} checks)")
    if meta_errors:
        lines.append("- ⚠️ Metadata API errors (results may be incomplete — treat "
                     "not_reflected with suspicion, re-run): " + "; ".join(meta_errors))
    if overall == "INCOMPLETE":
        lines.append("- Note: Metadata lineage lags republish (eventual consistency). "
                     "`partial` / `not_reflected` rows may simply be unpropagated — "
                     "re-run verify after a few minutes before treating them as a missed step.")
    lines.append("")

    if not n:
        lines.append("_No workbook checks in the design (empty `pairs`)._")
    else:
        lines.append("| Workbook | 旧 PDS → 新 PDS | 旧から消えた | 新に現れた | 判定 |")
        lines.append("|---|---|---|---|---|")
        rank = {"not_reflected": 0, "partial": 1, "reflected": 2}
        for r in sorted(results, key=lambda x: (rank[x["verdict"]], x["workbook"] or "")):
            mark = {"reflected": "✅ reflected", "partial": "🟡 partial",
                    "not_reflected": "❌ not_reflected"}[r["verdict"]]
            oc = "✅" if r["old_removed"] else "—"
            nc = "✅" if r["new_present"] else "—"
            lines.append(f"| {r['workbook']} | {r['old_pds']} → {r['new_pds']} | {oc} | {nc} | {mark} |")
        lines.append("")

        outstanding = [r for r in results if r["verdict"] != "reflected"]
        if outstanding:
            lines.append("## 未反映 / 部分反映 (要再確認)")
            lines.append("")
            for r in outstanding:
                detail = []
                if not r["old_removed"]:
                    detail.append("旧 PDS の downstream にまだ載っている")
                if not r["new_present"]:
                    detail.append("新 PDS の downstream にまだ載っていない")
                if not r["new_pds_seen"]:
                    detail.append("新 PDS 自体が lineage 未出現 (publish/反映待ちの可能性)")
                lines.append(f"- **{r['workbook']}** ({r['old_pds']} → {r['new_pds']}): "
                             + "; ".join(detail))
            lines.append("")
            lines.append("対応: 時間をおいて verify を再実行。数回リトライしても解消しない場合のみ "
                         "Desktop の Replace Data Source をやり直す (本 Skill は修正しない)。")
            lines.append("")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"[verify_repoint] wrote {out} (overall={overall})", file=sys.stderr)

    end = time.monotonic()
    print("RESULT_JSON: " + json.dumps({
        "status": "ok" if not meta_errors else "ok_with_metadata_errors",
        "overall_verdict": overall,
        "reflected": reflected,
        "partial": partial,
        "not_reflected": not_reflected,
        "checks": n,
        "unreflected_workbooks": sorted({r["workbook"] for r in results if r["verdict"] != "reflected"}),
        "out": str(out).replace("\\", "/"),
        "elapsed_s": round(end - t0),
        "breakdown": {"metadata_query": round(t_query - t0), "compare_write": round(end - t_query)},
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
