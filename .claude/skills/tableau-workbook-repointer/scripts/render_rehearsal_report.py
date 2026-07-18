#!/usr/bin/env python3
"""Render the rehearsal-gate approval report (Markdown + HTML, one pass).

Pure-local, no server access. Joins the machine outputs of the rehearsal run —
repoint_workbook.py's result JSON (--result-out) and one view-compare.json per
workbook (compare_workbook_views.py) — into an approval report a human can
read in under a minute. Both renderings come from the same computed data so
they cannot drift; the HTML (same stem as --out) is the primary artifact the
caller opens in a browser for the approval gate, the Markdown stays for the
caller/agent to quote inline.

Verdict model:

  - mechanical (decides READY_FOR_APPROVAL / NOT_READY): connection check,
    surgery token counts, candidate-side view export success.
  - eyeball material (listed, never auto-judged): per-view row counts and the
    embedded image pairs. Old-vs-new PDS data parity is verified upstream by
    tableau-pds-comparator before repoint, so it is not re-judged here;
    `baseline_export_failed` views (original broken, candidate exports) are
    listed as 要確認 without blocking.

Join key: repoint result `published_luid` == view-compare `candidate_workbook_luid`.

Usage:
    python render_rehearsal_report.py --repoint-result <repoint-result.json> \
        --compare <view-compare.json> [--compare <...> ...] \
        --out <output_dir>/repoint-rehearsal-report.md

Writes <out>.md and the sibling <out stem>.html. Final line: RESULT_JSON: {...}
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

VERDICT_MARK = {
    "ok": "✅ export OK",
    "export_failed": "❌ export 失敗 (両側)",
    "candidate_export_failed": "❌ copy 側 export 失敗 (手術不良疑い)",
    "baseline_export_failed": "⚠️ 元 WB 側 export 失敗 (既存破損の可能性 — copy は出力成功)",
    "only_in_one_workbook": "❌ view が片方にしかない",
}

# These view verdicts force NOT_READY. baseline_export_failed does not: the
# candidate exporting where the original cannot is an improvement to interpret,
# not a surgery defect.
BLOCKING_VERDICTS = {"export_failed", "candidate_export_failed", "only_in_one_workbook"}

READING_GUIDE = [
    "このレポートが保証するのは**配線**: 接続の切替 (旧 PDS 参照の残存ゼロ) と、copy 側の全 view が"
    "描画・export できること",
    "データ同値性 (列・値の parity) はこのゲートの管轄外 — repoint の事前条件として "
    "tableau-pds-comparator が旧 PDS vs 新 PDS で検証済みであることが前提",
    "行数と画像並置は「大きな崩れがないか」の目視確認材料。新旧 PDS の refresh タイミング差で"
    "値が微差になるのは想定内",
    "`元 WB 側 export 失敗` は元 WB が既に壊れている可能性 — copy が出力成功していれば "
    "repoint による改善。内容確認のうえ承認判断する",
]

NEXT_STEPS = [
    "承認 → repoint モードを `stage=production` で再起動 (元 WB へ Overwrite、WB LUID / URL は不変) "
    "→ verify モードで lineage 突合",
    "却下 → リハーサル copy は証拠として残る。design の再実行または手術対象の見直しへ",
    "注意: タグ・説明・Custom Views・購読が Overwrite で保持されるかは未検証 "
    "(重要 WB は本番前に人間が控えを取る)",
]


def jst_now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds")


def build_sections(rr: dict, compare_by_candidate: dict[str, dict],
                   compare_paths: dict[str, Path], out_dir: Path) -> tuple[list[dict], bool, list[str]]:
    """Compute per-workbook section data + overall readiness + attention list."""
    sections: list[dict] = []
    ready = True
    attention: list[str] = []
    for r in rr.get("results") or []:
        wb = r.get("workbook", "?")
        sec: dict = {
            "wb": wb,
            "ok": r.get("status") == "ok",
            "status": r.get("status", "?"),
            "copy_name": r.get("published_name", "?"),
            "copy_luid": r.get("published_luid", "?"),
            "url": r.get("webpage_url", ""),
            "original_url": r.get("original_webpage_url", ""),
            "counts": r.get("counts") or [],
            "warnings": r.get("warnings") or [],
            "errors": r.get("errors") or [],
            "stale_old_names": r.get("stale_old_names") or [],
            "missing_new_names": r.get("missing_new_names") or [],
            "views": [],
            "compare_html": None,
            "labels": ("baseline", "candidate"),
        }
        if not sec["ok"]:
            ready = False
            attention.append(f"{wb}: 手術/接続チェック失敗 ({sec['status']})")
            sections.append(sec)
            continue

        comp = compare_by_candidate.get(sec["copy_luid"])
        if comp is None:
            ready = False
            attention.append(f"{wb}: view 比較の証拠がない (compare 未実行)")
            sec["errors"].append("view 比較の証拠なし — compare_workbook_views.py を実行して再レンダーする")
            sections.append(sec)
            continue

        compare_dir = compare_paths[sec["copy_luid"]].parent
        sec["compare_html"] = os.path.relpath(
            compare_dir / "view-compare.html", out_dir).replace("\\", "/")
        sec["labels"] = (comp.get("label_baseline", "baseline"),
                         comp.get("label_candidate", "candidate"))

        def rel_png(rec: dict) -> str:
            png = rec.get("png")
            if not png:
                return ""
            return os.path.relpath(compare_dir / png, out_dir).replace("\\", "/")

        for v in comp.get("views") or []:
            verdict = v.get("verdict", "?")
            sec["views"].append({
                "view": v.get("view", "?"),
                "verdict": verdict,
                "base_rows": v.get("baseline", {}).get("rows", "—"),
                "cand_rows": v.get("candidate", {}).get("rows", "—"),
                "base_png": rel_png(v.get("baseline") or {}),
                "cand_png": rel_png(v.get("candidate") or {}),
            })
            if verdict in BLOCKING_VERDICTS:
                ready = False
                attention.append(f"{wb} / {v.get('view')}: {VERDICT_MARK.get(verdict, verdict)}")
            elif verdict == "baseline_export_failed":
                attention.append(f"{wb} / {v.get('view')}: 元 WB 側の export が失敗 "
                                 "(既存破損の可能性)。copy は出力成功 — 内容確認を")
        sections.append(sec)
    # Run-level warnings from repoint_workbook.py (e.g. pairs skipped in the
    # design) must reach the approver too, not just the machine payload.
    for w in rr.get("warnings") or []:
        attention.append(f"実行時警告: {w}")
    return sections, ready, attention


def counts_line(sec: dict) -> str:
    return "; ".join(
        f"{c['old']}: 接続属性 {c['content_url_attrs']} + 表示名属性 {c['name_attrs']}"
        for c in sec["counts"])


def render_md(meta: dict, sections: list[dict], overall: str, attention: list[str]) -> str:
    lines: list[str] = []
    lines.append("# Repoint リハーサル承認レポート")
    lines.append("")
    lines.append(f"- **機械判定: {overall}**")
    lines.append(f"- Generated at: {meta['generated_at']}")
    lines.append(f"- 対象 WB: {meta['total']} 件 (手術成功 {meta['ok']} / 失敗 {meta['failed']})")
    lines.append("")
    for sec in sections:
        lines.append(f"## {sec['wb']}")
        lines.append("")
        if sec["original_url"]:
            lines.append(f"- 元 WB (本番): {sec['original_url']}")
        lines.append(f"- リハーサル copy: **{sec['copy_name']}** (`{sec['copy_luid']}`)")
        if sec["url"]:
            lines.append(f"- copy URL: {sec['url']}")
        if sec["counts"]:
            lines.append("- 差し替え内容:")
            lines.append("")
            lines.append("| 旧 PDS | → | 新 PDS |")
            lines.append("|---|---|---|")
            for c in sec["counts"]:
                lines.append(f"| {c['old']} | → | **{c.get('new', '?')}** |")
            lines.append("")
        if not sec["ok"]:
            lines.append(f"- **手術結果: ❌ {sec['status']}**")
            for e in sec["errors"]:
                lines.append(f"  - {e}")
            if sec["stale_old_names"]:
                lines.append(f"  - 旧 PDS 参照が接続に残存: {', '.join(sec['stale_old_names'])}")
            if sec["missing_new_names"]:
                lines.append(f"  - 新 PDS が接続に不在: {', '.join(sec['missing_new_names'])}")
            lines.append("")
            continue
        lines.append("- 手術結果: ✅ 接続チェック PASS (旧 PDS 参照の残存なし・新 PDS を確認)")
        lines.append(f"- 置換: {counts_line(sec)}")
        for w in sec["warnings"]:
            lines.append(f"- ⚠️ {w}")
        for e in sec["errors"]:
            lines.append(f"- ❌ {e}")
        if sec["compare_html"]:
            lines.append(f"- view 比較 (画像並置): [view-compare.html]({sec['compare_html']})")
            lines.append("")
            la, lb = sec["labels"]
            lines.append(f"| view | export | {la} 行数 | {lb} 行数 |")
            lines.append("|---|---|---|---|")
            for v in sec["views"]:
                lines.append(f"| {v['view']} | {VERDICT_MARK.get(v['verdict'], v['verdict'])} | "
                             f"{v['base_rows']} | {v['cand_rows']} |")
        lines.append("")
    lines.append("## 判定の読み方")
    lines.append("")
    for g in READING_GUIDE:
        lines.append(f"- {g}")
    lines.append("")
    if attention:
        lines.append("## 要確認")
        lines.append("")
        for a in attention:
            lines.append(f"- {a}")
        lines.append("")
    lines.append("## 承認後の次ステップ")
    lines.append("")
    for s in NEXT_STEPS:
        lines.append(f"- {s}")
    lines.append("")
    return "\n".join(lines)


def render_html(meta: dict, sections: list[dict], overall: str, attention: list[str]) -> str:
    esc = html_mod.escape
    ready = overall == "READY_FOR_APPROVAL"
    badge_bg = "#1a7f37" if ready else "#c62828"
    verdict_color = {
        "ok": "#1a7f37",
        "baseline_export_failed": "#b26a00", "export_failed": "#c62828",
        "candidate_export_failed": "#c62828", "only_in_one_workbook": "#c62828",
    }
    parts: list[str] = []
    parts.append("<!doctype html><meta charset='utf-8'>")
    parts.append("<title>Repoint リハーサル承認レポート</title>")
    parts.append(
        "<style>"
        # 1400px page width fits two ~670px view images side by side — the
        # minimum at which chart labels stay readable without click-through.
        "body{font-family:'Segoe UI',sans-serif;margin:1.5rem auto;max-width:1400px;"
        "padding:0 1rem;color:#222;line-height:1.55}"
        "h1{font-size:1.5rem} h2{font-size:1.15rem;border-bottom:1px solid #ddd;"
        "padding-bottom:.2rem;margin-top:1.8rem}"
        ".badge{display:inline-block;color:#fff;font-weight:bold;border-radius:6px;"
        "padding:.35rem .9rem;font-size:1.05rem;background:" + badge_bg + "}"
        ".card{border:1px solid #ddd;border-radius:8px;padding: .8rem 1.1rem;margin:.8rem 0}"
        ".card.fail{border-color:#c62828;background:#fff5f5}"
        "table{border-collapse:collapse;margin:.5rem 0;width:100%}"
        "th,td{border:1px solid #ccc;padding:.35rem .6rem;text-align:left;font-size:.92rem}"
        "th{background:#f4f4f4}"
        "ul{margin:.3rem 0 .3rem 1.2rem;padding:0}"
        "a.btn{display:inline-block;border:1px solid #1565c0;color:#1565c0;border-radius:6px;"
        "padding:.2rem .7rem;text-decoration:none;font-size:.9rem;margin:.2rem 0}"
        "a.btn:hover{background:#e3f0fd}"
        ".warn{color:#b26a00} .err{color:#c62828} .okc{color:#1a7f37}"
        ".meta{color:#555;font-size:.9rem}"
        # Inline pairs: large enough to compare shapes and読める数値ラベル —
        # the goal is spotting gross differences; click-through gives full size.
        ".pair{display:flex;gap:.8rem;margin:.4rem 0 1.2rem}"
        ".pair figure{margin:0;flex:1;min-width:0;text-align:center}"
        ".pair img{max-width:100%;max-height:500px;object-fit:contain;"
        "border:1px solid #ccc;background:#fff}"
        ".pair figcaption{font-size:.78rem;color:#555;margin-top:.15rem}"
        ".pair .missing{display:flex;align-items:center;justify-content:center;"
        "min-height:80px;border:1px dashed #c62828;color:#c62828;font-size:.85rem}"
        ".viewname{font-size:.9rem;font-weight:bold;margin:.6rem 0 .1rem}"
        "</style>"
    )
    parts.append("<h1>Repoint リハーサル承認レポート</h1>")
    parts.append(f"<p><span class='badge'>機械判定: {esc(overall)}</span></p>")
    parts.append(f"<p class='meta'>Generated at: {esc(meta['generated_at'])} ／ "
                 f"対象 WB: {meta['total']} 件 (手術成功 {meta['ok']} / 失敗 {meta['failed']})</p>")

    for sec in sections:
        cls = "card" if sec["ok"] else "card fail"
        parts.append(f"<div class='{cls}'>")
        parts.append(f"<h2>{esc(sec['wb'])}</h2>")
        parts.append("<ul>")
        if sec["original_url"]:
            parts.append(f"<li>元 WB (本番): <a href='{esc(sec['original_url'])}'>Cloud で開く</a></li>")
        parts.append(f"<li>リハーサル copy: <b>{esc(sec['copy_name'])}</b> "
                     f"(<code>{esc(sec['copy_luid'])}</code>)"
                     + (f" — <a href='{esc(sec['url'])}'>Cloud で開く</a>" if sec["url"] else "")
                     + "</li>")
        if sec["counts"]:
            pairs_txt = "、".join(
                f"{esc(c['old'])} → <b>{esc(c.get('new', '?'))}</b>" for c in sec["counts"])
            parts.append(f"<li>差し替え内容: {pairs_txt}</li>")
        if not sec["ok"]:
            parts.append(f"<li class='err'><b>手術結果: ❌ {esc(sec['status'])}</b></li>")
            for e in sec["errors"]:
                parts.append(f"<li class='err'>{esc(e)}</li>")
            if sec["stale_old_names"]:
                parts.append(f"<li class='err'>旧 PDS 参照が接続に残存: "
                             f"{esc(', '.join(sec['stale_old_names']))}</li>")
            if sec["missing_new_names"]:
                parts.append(f"<li class='err'>新 PDS が接続に不在: "
                             f"{esc(', '.join(sec['missing_new_names']))}</li>")
            parts.append("</ul></div>")
            continue
        parts.append("<li class='okc'>手術結果: ✅ 接続チェック PASS "
                     "(旧 PDS 参照の残存なし・新 PDS を確認)</li>")
        parts.append(f"<li>置換: {esc(counts_line(sec))}</li>")
        for w in sec["warnings"]:
            parts.append(f"<li class='warn'>⚠️ {esc(w)}</li>")
        for e in sec["errors"]:
            parts.append(f"<li class='err'>❌ {esc(e)}</li>")
        parts.append("</ul>")
        if sec["compare_html"]:
            la, lb = sec["labels"]
            parts.append(f"<table><tr><th>view</th><th>export</th>"
                         f"<th>{esc(la)} 行数</th><th>{esc(lb)} 行数</th></tr>")
            for v in sec["views"]:
                color = verdict_color.get(v["verdict"], "#222")
                parts.append(
                    f"<tr><td>{esc(v['view'])}</td>"
                    f"<td style='color:{color}'>{esc(VERDICT_MARK.get(v['verdict'], v['verdict']))}</td>"
                    f"<td>{esc(str(v['base_rows']))}</td><td>{esc(str(v['cand_rows']))}</td></tr>")
            parts.append("</table>")
            for v in sec["views"]:
                color = verdict_color.get(v["verdict"], "#222")
                parts.append(f"<div class='viewname'>{esc(v['view'])} — "
                             f"<span style='color:{color}'>"
                             f"{esc(VERDICT_MARK.get(v['verdict'], v['verdict']))}</span></div>")
                parts.append("<div class='pair'>")
                for png, label in ((v["base_png"], la), (v["cand_png"], lb)):
                    if png:
                        parts.append(
                            f"<figure><a href='{esc(png)}'>"
                            f"<img src='{esc(png)}' alt='' loading='lazy'></a>"
                            f"<figcaption>{esc(label)} (クリックで原寸)</figcaption></figure>")
                    else:
                        parts.append(f"<figure><div class='missing'>export 失敗</div>"
                                     f"<figcaption>{esc(label)}</figcaption></figure>")
                parts.append("</div>")
            parts.append(f"<a class='btn' href='{esc(sec['compare_html'])}'>"
                         "🔍 フルサイズ並置ページを開く (view-compare.html)</a>")
        parts.append("</div>")

    parts.append("<h2>判定の読み方</h2><ul>")
    for g in READING_GUIDE:
        parts.append(f"<li>{esc(g)}</li>")
    parts.append("</ul>")
    if attention:
        parts.append("<h2>要確認</h2><ul>")
        for a in attention:
            parts.append(f"<li class='warn'>{esc(a)}</li>")
        parts.append("</ul>")
    parts.append("<h2>承認後の次ステップ</h2><ul>")
    for s in NEXT_STEPS:
        parts.append(f"<li>{esc(s)}</li>")
    parts.append("</ul>")
    return "".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repoint-result", required=True,
                    help="repoint_workbook.py --result-out JSON (stage must be rehearsal)")
    ap.add_argument("--compare", action="append", default=[], dest="compares",
                    help="view-compare.json (repeatable, one per workbook)")
    ap.add_argument("--out", required=True,
                    help="repoint-rehearsal-report.md path (the .html sibling is written too)")
    args = ap.parse_args()

    t0 = time.monotonic()
    rr = json.loads(Path(args.repoint_result).read_text(encoding="utf-8"))
    if rr.get("stage") != "rehearsal":
        sys.exit(f"ERROR: repoint result stage is {rr.get('stage')!r}, expected 'rehearsal' "
                 "(this report is the rehearsal approval gate)")

    compare_by_candidate: dict[str, dict] = {}
    compare_paths: dict[str, Path] = {}
    for cp in args.compares:
        c = json.loads(Path(cp).read_text(encoding="utf-8"))
        key = c.get("candidate_workbook_luid") or ""
        compare_by_candidate[key] = c
        compare_paths[key] = Path(cp)

    out_md = Path(args.out)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_html = out_md.with_suffix(".html")

    sections, ready, attention = build_sections(
        rr, compare_by_candidate, compare_paths, out_md.parent)
    overall = "READY_FOR_APPROVAL" if ready else "NOT_READY"
    meta = {
        "generated_at": jst_now_iso(),
        "total": rr.get("workbooks_ok", 0) + rr.get("workbooks_failed", 0),
        "ok": rr.get("workbooks_ok", 0),
        "failed": rr.get("workbooks_failed", 0),
    }

    out_md.write_text(render_md(meta, sections, overall, attention), encoding="utf-8")
    out_html.write_text(render_html(meta, sections, overall, attention), encoding="utf-8")
    print(f"[render_rehearsal_report] wrote {out_md} and {out_html} (overall={overall})",
          file=sys.stderr)
    print("RESULT_JSON: " + json.dumps({
        "status": "ok",
        "overall": overall,
        "attention_items": len(attention),
        "out": str(out_md).replace("\\", "/"),
        "html": str(out_html).replace("\\", "/"),
        "elapsed_s": round(time.monotonic() - t0),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
