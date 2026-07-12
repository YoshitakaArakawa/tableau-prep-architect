---
purpose: prep-workbook-repointer の入出力ファイルの「契約」と受け入れ基準。正確な構造は各スクリプトが唯一の source of truth で、ここでは二重管理しない
note: 消費者・保守者が知るべき最小限 (役割 / 依存される契約フィールド / 受け入れ基準 / 横断規範) のみ。JSON の全キー・runbook の全レンダリングは owning script を読む
---

# Repoint 入出力フォーマット

出力ファイルの**正確な形はスクリプトが生成し、それが唯一の source of truth**。本ファイルは全キーを再掲せず、(a) 各ファイルの役割と消費者、(b) 他が依存する契約フィールド、(c) 受け入れ基準と横断規範 だけを定める。

## ファイルと所有・消費

| ファイル | 生成 (= 正確な形の source) | 消費 |
|---|---|---|
| repoint-inventory.json | `inventory_workbooks.py` | `build_repoint_plan.py` のみ (中間・デバッグ用) |
| repoint-design.json | `build_repoint_plan.py` | `verify_repoint.py` + 人間 |
| repoint-runbook.md | `build_repoint_plan.py` | 人間 (Desktop 作業) |
| repoint-verify-report.md | `verify_repoint.py` | 人間 + caller |

design.json と runbook.md は `build_repoint_plan.py` が **1 パスで同時生成**するため内容は必ず一致 (食い違いは build のバグ)。

## design.json の契約フィールド (verify が依存)

`verify_repoint.py` が突合に使うキーだけは安定契約:
`pairs[].old_pds.luid` / `pairs[].new_pds.luid` / `pairs[].workbooks[].luid`。これらが揃えば verify は動く。

- `new_pds.luid` が null だと new-side 突合不能 → 先に manifest の resolve-luids を回す
- `match` (`"luid"` / `"name"`) は join の確からしさ (name は旧 PDS LUID が manifest に無く名前で救済したペア)
- `unmapped_old_pds` は差し替え先を確定できなかった旧 PDS (参照 WB 名付き)

他フィールドは `build_repoint_plan.py` を参照。

## runbook.md の受け入れ基準

「この 1 枚だけで Desktop の Replace Data Source を再現できる」こと。主役は **新 PDS *名*** と **WB *URL*** (Desktop は名前で選ぶため)。節の順序: 差し替え手順 (共通) → **WB ごと**の節 (URL + 旧→新 接続表、1 WB が複数旧 PDS を参照しうる) → 旧→新 PDS 全体表 → (あれば) unmapped 節。

## verify-report.md の判定語彙

判定の定義 (per-WB `reflected` / `partial` / `not_reflected`、overall `PASS` / `INCOMPLETE`) は
[lineage-model.md](lineage-model.md) が正典。report 固有の追加のみ: 対象 0 件は overall `EMPTY`。
未反映は「時間をおいて再実行」を案内し、原因 (lag か作業漏れか) は断定しない。

## 横断規範

- LUID・旧/新 PDS 名・WB URL を要約や「同上」で潰さない
- 個人情報 (owner / メール) を出さない
- 日付は yyyymmdd
