#!/usr/bin/env python3
"""Compare two workbooks view-by-view: export evidence + rendered images.

Rehearsal-gate evidence for repoint mode. Exports every view of a baseline
workbook (typically the untouched original) and a candidate workbook (the
rehearsal copy after TWB surgery) as live-queried CSV and freshly rendered
PNG, then emits:

  - view-compare.html  : side-by-side images per view (human)
  - view-compare.json  : per-view export verdicts + row counts (machine)

Views are matched by name. Verdicts are EXPORT-based only (data equality is
deliberately not judged here — old-vs-new PDS parity is tableau-pds-comparator's
job upstream, before repoint; refresh-timing value drift is expected):

  - ok                      : both sides exported (rows + images are the
                              human's eyeball material)
  - candidate_export_failed : copy broke where baseline works — surgery defect
  - baseline_export_failed  : the original itself is broken; the copy
                              exporting is an improvement to confirm
  - export_failed           : both sides failed
  - only_in_one_workbook    : view name mismatch between the two workbooks

Read-only. Usage:
    python compare_workbook_views.py --baseline <wb_luid> --candidate <wb_luid> \
        --out-dir <dir> [--label-baseline original] [--label-candidate repointed]

Final line: RESULT_JSON: {...}
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))

import tableauserverclient as TSC  # noqa: E402
from tableau_auth import signed_in_server  # noqa: E402


def slug(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name).strip("_").lower()


def export_workbook(server, wb_luid: str, prefix: str, out_dir: Path) -> dict[str, dict]:
    """Export each view as CSV (live query) + PNG (fresh render, maxage=1)."""
    wb = server.workbooks.get_by_id(wb_luid)
    out: dict[str, dict] = {}
    server.workbooks.populate_views(wb)
    for v in wb.views:
        rec: dict = {"csv": "", "png": "", "error": ""}
        try:
            server.views.populate_csv(v)
            csv_bytes = b"".join(v.csv)
            csv_name = f"{prefix}_{slug(v.name)}.csv"
            (out_dir / csv_name).write_bytes(csv_bytes)
            rec["csv"] = csv_name

            # maxage=1 forces a fresh render instead of a stale cached image.
            opts = TSC.ImageRequestOptions(
                imageresolution=TSC.ImageRequestOptions.Resolution.High, maxage=1)
            server.views.populate_image(v, opts)
            png_name = f"{prefix}_{slug(v.name)}.png"
            (out_dir / png_name).write_bytes(v.image)
            rec["png"] = png_name
        except Exception as e:  # a single broken view must not hide the others
            rec["error"] = str(e)
        out[v.name] = rec
        print(f"[compare_workbook_views] {prefix}: {v.name!r} "
              f"{'OK' if not rec['error'] else 'EXPORT FAILED: ' + rec['error']}",
              file=sys.stderr)
    return out


def csv_rows(path: Path) -> int:
    """Data-row count (header excluded). csv module handles quoted newlines."""
    text = path.read_bytes().decode("utf-8-sig", errors="replace")
    n = sum(1 for _ in csv.reader(io.StringIO(text)))
    return max(0, n - 1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--baseline", required=True, help="baseline workbook LUID")
    ap.add_argument("--candidate", required=True, help="candidate workbook LUID")
    ap.add_argument("--label-baseline", default="baseline")
    ap.add_argument("--label-candidate", default="candidate")
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    t0 = time.monotonic()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with signed_in_server() as server:
        base = export_workbook(server, args.baseline, "baseline", out_dir)
        cand = export_workbook(server, args.candidate, "candidate", out_dir)

    views = sorted(set(base) | set(cand))
    per_view: list[dict] = []
    for name in views:
        b, c = base.get(name), cand.get(name)
        if not (b and c):
            verdict = "only_in_one_workbook"
        elif b["error"] and c["error"]:
            verdict = "export_failed"
        elif c["error"]:
            # candidate broke where baseline works — surgery defect signal
            verdict = "candidate_export_failed"
        elif b["error"]:
            # baseline itself is broken (e.g. pre-existing stale reference);
            # the candidate exporting successfully is an improvement, not a defect
            verdict = "baseline_export_failed"
        else:
            verdict = "ok"
        rec = {
            "view": name, "verdict": verdict,
            "baseline": b or {}, "candidate": c or {},
        }
        if b and b.get("csv"):
            rec["baseline"]["rows"] = csv_rows(out_dir / b["csv"])
        if c and c.get("csv"):
            rec["candidate"]["rows"] = csv_rows(out_dir / c["csv"])
        per_view.append(rec)

    rows = []
    for r in per_view:
        cell = lambda rec: (f"<img src='{rec['png']}' alt=''>" if rec.get("png")
                            else f"<p>{html.escape(rec.get('error') or 'missing')}</p>")
        rows.append(
            f"<h2>{html.escape(r['view'])} — {r['verdict']}</h2>"
            f"<div class='pair'>"
            f"<figure><figcaption>{html.escape(args.label_baseline)}</figcaption>{cell(r['baseline'])}</figure>"
            f"<figure><figcaption>{html.escape(args.label_candidate)}</figcaption>{cell(r['candidate'])}</figure>"
            f"</div>"
        )
    page = (
        "<!doctype html><meta charset='utf-8'><title>view compare</title>"
        "<style>body{font-family:sans-serif;margin:1rem}"
        ".pair{display:flex;gap:1rem;align-items:flex-start;overflow-x:auto}"
        "figure{margin:0;flex:1;min-width:0}img{max-width:100%;border:1px solid #ccc}"
        "figcaption{font-weight:bold;margin-bottom:.3rem}</style>"
        f"<h1>{html.escape(args.label_baseline)} vs {html.escape(args.label_candidate)}</h1>"
        + "".join(rows)
    )
    (out_dir / "view-compare.html").write_text(page, encoding="utf-8")
    (out_dir / "view-compare.json").write_text(json.dumps({
        "baseline_workbook_luid": args.baseline,
        "candidate_workbook_luid": args.candidate,
        "label_baseline": args.label_baseline,
        "label_candidate": args.label_candidate,
        "views": per_view,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    tally = {k: sum(1 for r in per_view if r["verdict"] == k)
             for k in ("ok", "export_failed", "candidate_export_failed",
                       "baseline_export_failed", "only_in_one_workbook")}
    print("RESULT_JSON: " + json.dumps({
        "status": "ok" if tally["ok"] == len(per_view) else "attention",
        "views": len(per_view),
        **tally,
        "html": str(out_dir / "view-compare.html").replace("\\", "/"),
        "json": str(out_dir / "view-compare.json").replace("\\", "/"),
        "elapsed_s": round(time.monotonic() - t0),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
