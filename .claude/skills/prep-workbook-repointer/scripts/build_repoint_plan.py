#!/usr/bin/env python3
"""Join the workbook inventory against publish manifests -> repoint plan.

design-mode Steps 2-3, pure-local (no server access). Reads the inventory
(LEFT side: WB x old PDS) written by inventory_workbooks.py and the caller's
publish manifests (RIGHT side: old output PDS -> new marts PDS via
`source_original_output_name`), then emits BOTH design outputs from one pass so
they cannot drift:

  - repoint-design.json  (machine — verify-mode input)
  - repoint-runbook.md   (human — Desktop "Replace Data Source" procedure)

The old->new correspondence key is the OLD output PDS luid
(manifest.original.outputs[].luid == inventory old PDS luid). If a manifest's
original output luids are still null (resolve-luids not run), the join falls
back to matching by PDS name and flags it.

Usage:
    python build_repoint_plan.py --inventory <repoint-inventory.json> \
        --manifest <publish-manifest_1.json> [--manifest <...> ...] \
        --out-dir <output_dir>
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


def jst_today_compact() -> str:
    return datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d")


def build_mapping(manifest_paths: list[str]) -> tuple[dict[str, dict], dict[str, dict], list[str]]:
    """Compose old-output-PDS -> new-PDS maps from every manifest.

    Returns (by_old_luid, by_old_name, warnings). Each map value =
    {new_name, new_luid, source_flow, old_name, old_luid}.
    """
    by_old_luid: dict[str, dict] = {}
    by_old_name: dict[str, dict] = {}
    warnings: list[str] = []

    for mp in manifest_paths:
        m = json.loads(Path(mp).read_text(encoding="utf-8"))
        original = m.get("original") or {}
        orig_luid_by_name: dict[str, str | None] = {}
        for o in original.get("outputs") or []:
            orig_luid_by_name[o["name"]] = o.get("luid")

        for df in m.get("decomposed_flows") or []:
            src = df.get("source_original_output_name")
            if not src:
                # stg / intermediate helper PDS: no original output to repoint.
                continue
            new_outs = df.get("outputs") or []
            if not new_outs:
                warnings.append(
                    f"[{Path(mp).name}] decomposed flow '{df['name']}' maps to "
                    f"original output '{src}' but has no outputs; skipped"
                )
                continue
            new_out = new_outs[0]
            if len(new_outs) > 1:
                warnings.append(
                    f"[{Path(mp).name}] decomposed flow '{df['name']}' has "
                    f"{len(new_outs)} outputs; using the first ('{new_out.get('name')}')"
                )
            entry = {
                "new_name": new_out.get("name"),
                "new_luid": new_out.get("luid"),
                "source_flow": df.get("name"),
                "old_name": src,
                "old_luid": orig_luid_by_name.get(src),
            }
            if not new_out.get("luid"):
                warnings.append(
                    f"[{Path(mp).name}] new PDS '{new_out.get('name')}' has null "
                    f"LUID (run resolve-luids); verify keying will be name-only"
                )
            by_old_name[src] = entry
            old_luid = orig_luid_by_name.get(src)
            if old_luid:
                by_old_luid[old_luid] = entry
            else:
                warnings.append(
                    f"[{Path(mp).name}] original output '{src}' has null LUID; "
                    f"old->new join for it will fall back to name matching"
                )
    return by_old_luid, by_old_name, warnings


def build_pairs(inventory: dict, by_old_luid: dict, by_old_name: dict,
                warnings: list[str]) -> tuple[list[dict], list[dict]]:
    """Return (pairs, unmapped). One pair per mapped old PDS."""
    new_index = inventory.get("new_pds_index") or {}
    pairs: list[dict] = []
    unmapped: list[dict] = []

    for p in inventory.get("old_pds") or []:
        old_luid = p.get("luid")
        old_name = p.get("name")
        entry = None
        match = None
        if old_luid and old_luid in by_old_luid:
            entry = by_old_luid[old_luid]
            match = "luid"
        elif old_name in by_old_name:
            entry = by_old_name[old_name]
            match = "name"
            warnings.append(
                f"old PDS '{old_name}' matched to new PDS by NAME (its LUID was "
                f"not in any manifest original.outputs); verify the mapping"
            )

        wbs = [
            {
                "luid": wb.get("luid"),
                "name": wb.get("name"),
                "project_name": wb.get("project_name"),
                "webpage_url": wb.get("webpage_url", ""),
            }
            for wb in (p.get("downstream_workbooks") or [])
        ]

        if entry is None:
            unmapped.append({
                "luid": old_luid,
                "name": old_name,
                "project_name": p.get("project_name"),
                "workbook_count": len(wbs),
                "workbook_names": [wb["name"] for wb in wbs],
            })
            continue

        new_luid = entry.get("new_luid")
        content_url = ""
        if new_luid and new_luid in new_index:
            content_url = new_index[new_luid].get("content_url", "")

        pairs.append({
            "old_pds": {
                "luid": old_luid,
                "name": old_name,
                "project_name": p.get("project_name"),
            },
            "new_pds": {
                "luid": new_luid,
                "name": entry.get("new_name"),
                "content_url": content_url,
            },
            "source_flow_name": entry.get("source_flow"),
            "match": match,
            "workbooks": wbs,
        })

    # Stable, human-friendly ordering by old PDS name.
    pairs.sort(key=lambda x: (x["old_pds"]["name"] or "").lower())
    unmapped.sort(key=lambda x: (x["name"] or "").lower())
    return pairs, unmapped


def render_runbook(design: dict) -> str:
    src_project = design["source_project"]
    pairs = design["pairs"]
    unmapped = design["unmapped_old_pds"]

    # WB-centric view: Desktop's Replace Data Source is driven per workbook, and
    # one workbook may consume several old PDS (several connections to replace).
    wb_map: dict[str, dict] = {}
    wb_order: list[str] = []
    for pr in pairs:
        for wb in pr["workbooks"]:
            luid = wb["luid"]
            if luid not in wb_map:
                wb_map[luid] = {
                    "name": wb["name"],
                    "project_name": wb["project_name"],
                    "webpage_url": wb["webpage_url"],
                    "replacements": [],
                }
                wb_order.append(luid)
            wb_map[luid]["replacements"].append({
                "old_name": pr["old_pds"]["name"],
                "new_name": pr["new_pds"]["name"],
            })
    wb_order.sort(key=lambda l: (wb_map[l]["name"] or "").lower())

    lines: list[str] = []
    lines.append("---")
    lines.append("title: Workbook Repoint 設計 (対応表 + Desktop fallback 手順)")
    lines.append(f"created_at: {design['created_at']}")
    lines.append("scope: 設計資料。差し替えの既定は repoint モード (TWB 手術による自動差し替え、"
                 "rehearsal → 承認 → production)。本書の Desktop 手順は手術不可ケース・"
                 "権限制約時の fallback")
    lines.append(f"source_of_truth: publish-manifest.json ({', '.join(design['manifest_paths'])}) "
                 "+ Metadata API lineage inventory")
    lines.append(f"site: {design.get('server','')} / site {design.get('site_name','')!r}")
    lines.append("---")
    lines.append("")
    lines.append("# Workbook Repoint 設計")
    lines.append("")
    lines.append(
        f"移行後、`{src_project}` 配下の旧 Published Data Source を参照する Workbook を、"
        "分解後フローが出力する新 marts PDS へ差し替えるための資料。**利用状況では絞り込んでいない** — "
        "デモ / 拡張系も含め、旧 PDS を参照する Workbook を全件掲載する (取捨は人間判断)。"
        "差し替えの**既定は repoint モード** (TWB 手術による自動差し替え。rehearsal → 承認レポート → "
        "production の段取りゲート付き)。手術が停止したケースや自動 republish を許可しない運用では、"
        "下記手順どおり Tableau Desktop の **データソースの置換** (新 PDS を *名前* で選択) に "
        "fallback する。"
    )
    lines.append("")
    lines.append(f"- 対象 Workbook: **{len(wb_order)} 件** / 旧→新 PDS ペア: **{len(pairs)} 件**")
    if unmapped:
        lines.append(f"- ⚠️ 対応先が見つからない旧 PDS: **{len(unmapped)} 件** (末尾セクション)")
    lines.append("")

    lines.append("## Desktop 差し替え手順 (fallback 用・全 Workbook 共通)")
    lines.append("")
    lines.append("1. Tableau Desktop で対象 Workbook を開く (下表の URL から)")
    lines.append("2. メニュー [データ] → [データソースの置換]")
    lines.append("3. 現在のデータソース = 旧 PDS、置換後 = 新 PDS (下表「新 PDS 名」で選択)")
    lines.append("4. Workbook 内のすべての接続を置換したら republish (サーバーへ上書き保存)")
    lines.append("5. 全 Workbook 完了後、本 Skill の verify モードで lineage 反映を突合する")
    lines.append("")

    lines.append(f"## 対象 Workbook 一覧 ({len(wb_order)} 件)")
    lines.append("")
    if not wb_order:
        lines.append("_該当なし (旧 PDS を参照する Workbook が見つからなかった)。_")
        lines.append("")
    for luid in wb_order:
        wb = wb_map[luid]
        proj = wb["project_name"] or "?"
        lines.append(f"### {wb['name']}  (`{proj}`)")
        lines.append("")
        url = wb["webpage_url"] or "_(URL 解決不可 — Cloud UI で検索)_"
        lines.append(f"- URL: {url}")
        lines.append(f"- WB LUID: `{luid}`")
        lines.append("- 差し替える接続:")
        lines.append("")
        lines.append("| 旧 PDS 名 (現在) | → | 新 PDS 名 (置換後) |")
        lines.append("|---|---|---|")
        for rep in wb["replacements"]:
            lines.append(f"| {rep['old_name']} | → | **{rep['new_name']}** |")
        lines.append("")

    lines.append(f"## 旧 PDS → 新 PDS 対応 (全体, {len(pairs)} ペア)")
    lines.append("")
    lines.append("| 旧 PDS | → | 新 PDS | 由来フロー | 参照 WB 数 | 対応キー |")
    lines.append("|---|---|---|---|---|---|")
    for pr in pairs:
        lines.append(
            f"| {pr['old_pds']['name']} | → | **{pr['new_pds']['name']}** | "
            f"{pr['source_flow_name']} | {len(pr['workbooks'])} | {pr['match']} |"
        )
    lines.append("")

    if unmapped:
        lines.append(f"## ⚠️ 対応先が見つからない旧 PDS ({len(unmapped)} 件)")
        lines.append("")
        lines.append(
            "以下の旧 PDS は Workbook から参照されているが、渡された manifest に "
            "対応する分解後フロー (`source_original_output_name`) が無く、差し替え先を "
            "機械確定できなかった。移行対象外か、manifest の渡し漏れ / resolve-luids 未実行の "
            "可能性がある。人間が確認する。"
        )
        lines.append("")
        lines.append("| 旧 PDS | 参照 WB 数 | 参照 WB | LUID |")
        lines.append("|---|---|---|---|")
        for u in unmapped:
            wb_names = ", ".join(u.get("workbook_names") or []) or "—"
            lines.append(f"| {u['name']} | {u['workbook_count']} | {wb_names} | `{u['luid']}` |")
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--inventory", required=True, help="repoint-inventory.json")
    ap.add_argument("--manifest", action="append", default=[], dest="manifests",
                    required=True, help="publish-manifest.json (repeatable)")
    ap.add_argument("--out-dir", required=True, help="Directory for repoint-design.json + repoint-runbook.md")
    args = ap.parse_args()

    inv_path = Path(args.inventory)
    if not inv_path.is_file():
        sys.exit(f"ERROR: inventory not found: {inv_path}")
    inventory = json.loads(inv_path.read_text(encoding="utf-8"))

    by_old_luid, by_old_name, warnings = build_mapping(args.manifests)
    pairs, unmapped = build_pairs(inventory, by_old_luid, by_old_name, warnings)
    # de-dup warnings while preserving order
    warnings = list(dict.fromkeys(warnings + (inventory.get("warnings") or [])))

    design = {
        "schema_version": "1",
        "created_at": jst_today_compact(),
        "generated_at": jst_now_iso(),
        "server": inventory.get("server", ""),
        "site_name": inventory.get("site_name", ""),
        "source_project": inventory.get("source_project", ""),
        "manifest_paths": [str(Path(m)).replace("\\", "/") for m in args.manifests],
        "pairs": pairs,
        "unmapped_old_pds": unmapped,
        "warnings": warnings,
    }

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    design_path = out_dir / "repoint-design.json"
    runbook_path = out_dir / "repoint-runbook.md"
    design_path.write_text(json.dumps(design, ensure_ascii=False, indent=2), encoding="utf-8")
    runbook_path.write_text(render_runbook(design), encoding="utf-8")

    for w in warnings:
        print(f"[build_repoint_plan] WARNING: {w}", file=sys.stderr)
    print(f"[build_repoint_plan] wrote {design_path} and {runbook_path}", file=sys.stderr)

    total_wbs = len({wb["luid"] for pr in pairs for wb in pr["workbooks"]})
    print("RESULT_JSON: " + json.dumps({
        "status": "ok",
        "pairs": len(pairs),
        "workbooks": total_wbs,
        "unmapped_old_pds": len(unmapped),
        "warnings": len(warnings),
        "design": str(design_path).replace("\\", "/"),
        "runbook": str(runbook_path).replace("\\", "/"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
