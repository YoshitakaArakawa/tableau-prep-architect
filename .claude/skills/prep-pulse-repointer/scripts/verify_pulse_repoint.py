#!/usr/bin/env python3
"""Verify Pulse repoint: rescan definitions and reconcile against the design.

verify-mode. For each design pair, PASS requires all of:
  - a definition with the ORIGINAL name exists and references the NEW PDS
  - follower count on the new definition >= the archived old definition's LIVE
    subscription count (followers drift after design, so the design snapshot is
    not the reference; once the old definition is deleted the check is skipped)
  - the new definition's insight probe returns a result (field parity holds)

Leftover "<name> (pre-repoint)" and "<name> (repoint rehearsal)" definitions
are listed as warnings only — their deletion cascades to metrics/subscriptions
and stays a human decision.

Usage:
    python verify_pulse_repoint.py --design <pulse-repoint-design.json> \
        --out <output_dir>/pulse-repoint-verify-report.md

Cloud access is read-only (insight generation persists nothing).
Final line: RESULT_JSON: {...}
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tableau_auth import signed_in_server  # noqa: E402

from pulse_api import insight_probe, list_definitions, list_subscriptions  # noqa: E402

REHEARSAL_SUFFIX = " (repoint rehearsal)"
ARCHIVE_SUFFIX = " (pre-repoint)"


def jst_now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--design", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    t0 = time.monotonic()

    design = json.loads(Path(args.design).read_text(encoding="utf-8"))

    with signed_in_server() as server:
        all_defs = list_definitions(server)
        by_name: dict[str, list[dict]] = {}
        for d in all_defs:
            by_name.setdefault(d["metadata"]["name"], []).append(d)

        rows, leftovers, discard_rows = [], [], []
        for pair in design["pairs"]:
            name = pair["definition_name"]
            # Discard candidates (no followers at design time) are not part of
            # the migration verdict — unless the user promoted them (a new-PDS
            # definition under the original name exists). For unpromoted ones,
            # watch the still-live old definition for LATE followers: a follower
            # appearing after design means "unused" no longer holds.
            scope = pair.get("migration_scope", "followed")
            promoted = any(d["specification"]["datasource"]["id"] == pair["new_pds"]["luid"]
                           for d in by_name.get(name, []))
            if scope == "unfollowed" and not promoted:
                late = sum(
                    len(list_subscriptions(server, metric_id=m["id"]))
                    for d in by_name.get(name, [])
                    if d["specification"]["datasource"]["id"] == pair["old_pds"]["luid"]
                    for m in d.get("metrics", []))
                discard_rows.append({"name": name, "late_followers": late})
                continue
            # Expected followers come from the archived old definition's LIVE
            # subscriptions (followers drift after design). Once the old
            # definition is deleted (post-cutover) there is nothing to compare
            # against, so the follower check is skipped for that pair.
            old_matches = [d for d in by_name.get(name + ARCHIVE_SUFFIX, [])
                           if d["specification"]["datasource"]["id"] == pair["old_pds"]["luid"]]
            if old_matches:
                expected_followers = sum(
                    len(list_subscriptions(server, metric_id=m["id"]))
                    for m in old_matches[0].get("metrics", []))
            else:
                expected_followers = None  # old definition gone: skip the check
            expected_label = "-" if expected_followers is None else str(expected_followers)
            new_matches = [d for d in by_name.get(name, [])
                           if d["specification"]["datasource"]["id"] == pair["new_pds"]["luid"]]
            if not new_matches:
                rows.append({"name": name, "verdict": "FAIL",
                             "detail": "元の名前で新 PDS 参照の定義が見つからない",
                             "followers": f"?/{expected_label}", "insight": "-"})
                continue
            new_def = new_matches[0]
            actual_followers = sum(
                len(list_subscriptions(server, metric_id=m["id"]))
                for m in new_def.get("metrics", []))
            metrics = new_def.get("metrics", [])
            probe_metric = next((m for m in metrics if m.get("is_default")), metrics[0] if metrics else None)
            ok, markup = insight_probe(server, new_def, probe_metric) if probe_metric else (False, "no metric")
            follower_ok = expected_followers is None or actual_followers >= expected_followers
            verdict = "PASS" if (ok and follower_ok) else "FAIL"
            detail = []
            if not follower_ok:
                detail.append("follower 不足 (旧定義のライブ購読 > 新定義の購読)")
            if not ok:
                detail.append(f"insight 失敗: {markup[:120]}")
            rows.append({"name": name, "verdict": verdict,
                         "detail": " / ".join(detail) or "-",
                         "followers": f"{actual_followers}/{expected_label}",
                         "insight": "ok" if ok else "failed"})
        for d in all_defs:
            n = d["metadata"]["name"]
            if n.endswith(ARCHIVE_SUFFIX) or n.endswith(REHEARSAL_SUFFIX):
                leftovers.append(f"{n} ({d['metadata']['id']})")

    if not rows:
        overall = "EMPTY"
    elif all(r["verdict"] == "PASS" for r in rows):
        overall = "PASS"
    else:
        overall = "INCOMPLETE"

    lines = [
        "# Pulse repoint verify report",
        "",
        f"generated_at: {jst_now_iso()}",
        f"overall_verdict: **{overall}**",
        "",
        "| Pulse 定義 | verdict | follower (実測/期待) | insight | 詳細 |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| {r['name']} | {r['verdict']} | {r['followers']} |"
                     f" {r['insight']} | {r['detail']} |")
    lines += ["", "## 破棄候補 (follower なし・未移行 — 判定対象外)", ""]
    if discard_rows:
        for r in discard_rows:
            mark = (f" ⚠️ **後発 follower {r['late_followers']} 名が出現 — 未使用前提が崩れた。"
                    f"昇格 (repoint) を検討**" if r["late_followers"] else "")
            lines.append(f"- {r['name']}: 未移行 (カットオーバー時に旧定義ごと削除予定){mark}")
    else:
        lines.append("- なし")
    lines += ["", "## 残存 warning (削除は人間判断 — 削除で metrics/subscriptions が連鎖削除される)", ""]
    lines += [f"- {x}" for x in leftovers] or ["- なし"]
    fails = [r["name"] for r in rows if r["verdict"] == "FAIL"]
    late_names = [r["name"] for r in discard_rows if r["late_followers"]]
    lines += ["", "## 要対応", ""]
    todo = [f"- {n}: repoint モードの再実行または design の再確認" for n in fails]
    todo += [f"- {n}: 破棄候補に後発 follower — 昇格 (repoint) するか削除前に本人確認" for n in late_names]
    lines += todo or ["- なし"]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = {"out": str(out_path), "overall_verdict": overall,
              "pass": sum(1 for r in rows if r["verdict"] == "PASS"),
              "fail": len(fails), "leftovers": len(leftovers),
              "discard_candidates": len(discard_rows),
              "discard_with_late_followers": len(late_names),
              "elapsed_s": round(time.monotonic() - t0, 1)}
    print(f"RESULT_JSON: {json.dumps(result, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
