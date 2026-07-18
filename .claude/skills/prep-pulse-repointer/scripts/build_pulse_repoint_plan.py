#!/usr/bin/env python3
"""Build the pulse-repoint plan: join the inventory onto publish-manifests.

design-mode Step 2, local-only (no server access). RIGHT side of the join:
old PDS luid <-> manifest original.outputs[].luid -> source_original_output_name
-> the decomposed flow's new output PDS (same join key as prep-workbook-repointer).
Falls back to name matching when the manifest's old-output luid is null, marked
`match: "name"`. Old PDS with no mapping land in `unmapped_old_pds`.
Canonical join-model spec: references/publish-manifest-format.md section
"repoint join model" (shared with build_repoint_plan.py — keep in sync).

Emits pulse-repoint-design.json (machine input for repoint/verify) and
pulse-repoint-runbook.md (human) in one pass so the two never diverge.

Usage:
    python build_pulse_repoint_plan.py \
        --inventory <output_dir>/pulse-repoint-inventory.json \
        --manifest <publish-manifest_1.json> [--manifest <...> ...] \
        --out-dir <output_dir>

Final line: RESULT_JSON: {...}
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


def jst_now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds")


def load_manifest_maps(manifest_paths: list[str]) -> tuple[dict, dict]:
    """Return (by_old_luid, by_old_name) -> {new_luid, new_name, match}."""
    by_luid: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for path in manifest_paths:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
        old_outputs = (manifest.get("original") or {}).get("outputs") or []
        old_by_name = {o.get("name"): o for o in old_outputs}
        for flow in manifest.get("decomposed_flows", []):
            src_name = flow.get("source_original_output_name")
            if not src_name:
                continue
            new_outputs = flow.get("outputs") or []
            if not new_outputs:
                continue
            new = {"new_luid": new_outputs[0].get("luid"),
                   "new_name": new_outputs[0].get("name")}
            old = old_by_name.get(src_name) or {}
            if old.get("luid"):
                by_luid[old["luid"]] = new
            by_name[src_name] = new
    return by_luid, by_name


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--manifest", action="append", required=True)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()
    t0 = time.monotonic()

    inventory = json.loads(Path(args.inventory).read_text(encoding="utf-8"))
    by_luid, by_name = load_manifest_maps(args.manifest)

    pairs, unmapped, out_of_scope = [], {}, []
    for d in inventory["definitions"]:
        if not d["in_scope"]:
            out_of_scope.append({
                "definition_name": d["name"],
                "datasource_name": d["datasource_name"],
                "project": d["datasource_project"],
            })
            continue
        hit, match = None, None
        if d["datasource_id"] in by_luid:
            hit, match = by_luid[d["datasource_id"]], "luid"
        elif d["datasource_name"] in by_name:
            hit, match = by_name[d["datasource_name"]], "name"
        if not hit or not hit.get("new_luid"):
            unmapped.setdefault(d["datasource_id"], {
                "luid": d["datasource_id"], "name": d["datasource_name"], "definitions": [],
            })["definitions"].append(d["name"])
            continue
        followers_total = sum(len(m["followers"]) for m in d["metrics"])
        pairs.append({
            "definition_id": d["definition_id"],
            "definition_name": d["name"],
            "old_pds": {"luid": d["datasource_id"], "name": d["datasource_name"]},
            "new_pds": {"luid": hit["new_luid"], "name": hit["new_name"], "match": match},
            "referenced_fields": d["referenced_fields"],
            # follower presence at design time tiers the runbook: followed ->
            # migrate; unfollowed -> discard candidate (human can promote).
            # Production re-reads followers live, and verify watches unfollowed
            # pairs for late followers, so this snapshot is advisory not final.
            "followers_total": followers_total,
            "migration_scope": "followed" if followers_total else "unfollowed",
            "non_default_metrics": [
                {"metric_id": m["metric_id"], "specification": m["specification"],
                 "followers": m["followers"]}
                for m in d["metrics"] if not m["is_default"]
            ],
            "default_metric_followers": [
                f for m in d["metrics"] if m["is_default"] for f in m["followers"]
            ],
        })

    design = {
        "generated_at": jst_now_iso(),
        "source_project": inventory["source_project"],
        "pairs": pairs,
        "unmapped_old_pds": list(unmapped.values()),
        "out_of_scope": out_of_scope,
    }
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    design_path = out_dir / "pulse-repoint-design.json"
    design_path.write_text(json.dumps(design, indent=2, ensure_ascii=False), encoding="utf-8")

    followed = [p for p in pairs if p["migration_scope"] == "followed"]
    unfollowed = [p for p in pairs if p["migration_scope"] == "unfollowed"]

    def impact_row(p):
        followers = p["default_metric_followers"] + [
            f for m in p["non_default_metrics"] for f in m["followers"]]
        follower_ids = sorted({f.get("user_id", f.get("group_id", "?")) for f in followers})
        n_metrics = len(p["non_default_metrics"])
        note = "" if p["new_pds"]["match"] == "luid" else " (name 一致)"
        return (
            f"| {p['definition_name']} | `{p['definition_id']}` | {p['old_pds']['name']} | "
            f"{p['new_pds']['name']}{note} | {n_metrics} 本 | "
            f"{len(follower_ids)} 名 ({', '.join(i[:8] for i in follower_ids) or '-'}) | "
            f"旧定義 + {n_metrics + 1} metric + {len(followers)} 購読 |")

    header = ("| Pulse 定義 | 旧定義 id | 旧 PDS | → 新 PDS | 再作成される scoped metric |"
              " follower (再購読対象) | カットオーバーで消えるもの |")
    sep = "|---|---|---|---|---|---|---|"
    lines = [
        "# Pulse repoint runbook (go/no-go 判断書)",
        "",
        f"generated_at: {design['generated_at']} / source_project: {design['source_project']}",
        "",
        "この 1 枚で「今回の移行で影響を受ける Pulse 資産の全量」と「引き継がれないもの・",
        "残余リスク」を確認し、repoint を進めてよいか判断する。",
        "",
        "repoint は copy-promote 方式 (in-place の datasource 差し替えは API 不可) のため、",
        "**定義 id が変わる**。新 id は production 実行後に確定し verify レポートに記載される。",
        "",
        f"## Impact 1: 移行対象 — follower あり ({len(followed)} 定義)",
        "",
        "利用シグナル (follower) があり、新 PDS への repoint (metric + follower 移行) を行う定義。",
        "",
        header,
        sep,
    ]
    lines += [impact_row(p) for p in followed] or ["| (なし) | | | | | | |"]
    lines += [
        "",
        f"## Impact 2: 破棄候補 — follower なし ({len(unfollowed)} 定義)",
        "",
        "design 時点で follower がおらず **未使用とみなせる** 定義。既定では repoint せず、",
        "カットオーバー時に旧定義ごと削除する (旧 PDS を退役すると参照切れで壊れるため放置は不可)。",
        "⚠️ follower ゼロは完全な未使用の保証ではない (埋め込み表示・作成者の直接閲覧は follow",
        "不要)。残したい定義があればユーザー判断で移行対象に**昇格**できる (repoint モードに",
        "定義 id を渡すだけ)。verify は破棄候補の旧定義に後から follower が現れていないかを監視する。",
        "",
        header,
        sep,
    ]
    lines += [impact_row(p) for p in unfollowed] or ["| (なし) | | | | | | |"]
    lines += [
        "",
        "参照フィールド (新 PDS に同名で存在する必要がある — 不整合は insight 生成時に顕在化):",
        "",
    ]
    for p in pairs:
        scope = "移行" if p["migration_scope"] == "followed" else "破棄候補"
        lines.append(f"- [{scope}] {p['definition_name']}: {', '.join(p['referenced_fields'])}")
    lines += [
        "",
        "## 引き継がれないもの",
        "",
        "- **定義 id / metric id**: ブックマーク・埋め込み URL は旧 id を指したままになる (張り直しが必要)",
        "- **insight 履歴・digest の学習状態**: 新定義でリセットされ、日数の蓄積で再形成される",
        "- 猶予期間中 (旧定義削除まで) は follower の Following / digest に新旧が**二重表示**される",
        "",
        "## 対象外 (スコープ外 PDS 参照)",
        "",
        "| Pulse 定義 | 参照 PDS | project |",
        "|---|---|---|",
    ]
    for o in out_of_scope:
        lines.append(f"| {o['definition_name']} | {o['datasource_name']} | {o['project']} |")
    lines += [
        "",
        "## 段取り",
        "",
        "1. repoint モード stage=rehearsal — 各定義の rehearsal コピーを新 PDS で作成し、",
        "   元定義とコピーの insight を比較 (元定義は無傷)",
        "2. ユーザーが rehearsal の証拠 (insight 比較) を確認して明示承認",
        "3. repoint モード stage=production — 旧定義を `(pre-repoint)` に rename → rehearsal",
        "   コピーを元の名前に昇格 → metric / follower 購読を再作成 → insight 検証。",
        "   **follower は design 時点でなく実行時に旧定義から読み直してミラーする**",
        "   (design 後に増えた後発 follower の移行漏れを防ぐ)",
        "4. verify モードでサーバー実測と突合 (PASS を確認)",
        "5. 旧定義 `<名> (pre-repoint)` の削除は人間が実施 (UI または",
        "   `DELETE /api/-/pulse/definitions/{id}`)。**削除で配下 metrics + subscriptions が",
        "   連鎖削除される** — 必ず下記チェックリスト完了後に行う",
        "",
        "## 残余リスク (注意喚起 — 本エージェントには列挙・保証できない)",
        "",
        "- **旧 PDS の直接利用**: Tableau Desktop / Web authoring / 外部 API から旧 PDS に直接",
        "  接続している利用は lineage に写らず列挙できない。**旧 PDS を削除する前に利用者への",
        "  周知期間を置くこと**",
        "- **新 PDS の閲覧権限**: follower / 閲覧者が新 PDS (marts プロジェクト) を参照できるかは",
        "  プロジェクト権限設定の問題で本エージェントの責務外。**ユーザー側で権限を確認すること**",
        "",
        "## カットオーバー前チェックリスト (旧定義削除の前提)",
        "",
        "- [ ] verify モードが移行対象の全定義で PASS",
        "- [ ] follower 全員が新定義側に再購読済み (verify の follower 突合で確認)",
        "- [ ] 破棄候補の扱いを確定した (削除でよいか / 昇格して移行するか。verify の後発",
        "      follower 警告も確認)",
        "- [ ] 残余リスク 2 点 (直接利用の周知 / 新 PDS 権限) を確認した",
        "",
        "## unmapped / warnings",
        "",
    ]
    if unmapped:
        for u in unmapped.values():
            lines.append(f"- 旧 PDS `{u['name']}` ({u['luid']}) の新 PDS が manifest から確定できない"
                         f" (定義: {', '.join(u['definitions'])})。manifest の渡し漏れ /"
                         f" resolve-luids 未了を確認")
    else:
        lines.append("- なし")
    runbook_path = out_dir / "pulse-repoint-runbook.md"
    runbook_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = {
        "design": str(design_path),
        "runbook": str(runbook_path),
        "pairs": len(pairs),
        "followed": len(followed),
        "unfollowed_discard_candidates": len(unfollowed),
        "unmapped_old_pds": len(unmapped),
        "out_of_scope": len(out_of_scope),
        "elapsed_s": round(time.monotonic() - t0, 1),
    }
    print(f"RESULT_JSON: {json.dumps(result, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
