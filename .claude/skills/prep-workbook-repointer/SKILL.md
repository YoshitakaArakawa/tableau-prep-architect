---
name: prep-workbook-repointer
description: 移行後、旧 Published Data Source を参照する Workbook を新 marts PDS へ差し替えるための設計資料 (repoint-runbook.md + repoint-design.json) を生成し、差し替え後に反映を検証する Skill。左辺 (どの WB がどの旧 PDS を参照するか) は Metadata API の downstreamWorkbooks で、右辺 (旧 PDS → 新 fct PDS の対応) は publish-manifest の source_original_output_name で機械確定し、Tableau Desktop の Replace Data Source で人間が名前選択で差し替えるための対象 WB URL・新旧 PDS 名・手順を 1 枚にまとめる。人間の差し替え後は verify モードでサーバー実測 lineage と突合する。移行完了後に「WB の参照置換」「workbook を新 PDS に差し替え」「repoint の設計資料を作って」「参照置換を検証して」と言われたときに起動。接続の書き換え自体はしない (Replace Data Source は人間の UI 作業)。Cloud は読み取りのみ。
context: fork
agent: general-purpose
allowed-tools: Read Write Bash(python *) Glob Grep
---

# prep-workbook-repointer

移行後、旧 PDS を参照する Workbook を新 marts PDS へ差し替える作業を **設計** (design モード) し、
人間が Tableau Desktop の Replace Data Source で差し替えた後に **検証** (verify モード) する Skill。
**読み取り専用** (Cloud への書き込み副作用なし)。設計モデル・判断根拠は
[references/lineage-model.md](references/lineage-model.md)、出力形式は
[references/repoint-format.md](references/repoint-format.md)。

存在理由: 「どの WB を・どの接続を・どの新 PDS *名* に差し替えるか」を Metadata API lineage と
publish-manifest の突合で **機械確定** し、人間が UI で再現できる 1 枚の runbook にすること。
接続の実書き換え (`.twb` 編集 / republish) は **スコープ外** — 人間が Desktop で行う。差し替え後は
同じ lineage を再走査して反映を verify する。

役割対称性: 読み取り = prep-extractor + prep-output-comparator + prep-schedule-designer +
**prep-workbook-repointer** / 書き込み = prep-deployer (+ augmenter, backfiller)。

## Caller から渡される入力

`context: fork` で動くため caller (メインエージェント) は起動時に以下を文章で明示すること:

| 入力 | モード | 必須 | 説明 |
|---|---|---|---|
| `mode` | — | ✅ | `design` または `verify` |
| `manifest_paths` | design | ✅ | 全移行セッションの publish-manifest JSON パスの**明示列挙** (glob しない — 別セッション残骸の混入防止)。複数フローは複数 work フォルダに分散するので caller が集約して渡す。形式は [publish-manifest-format.md](../../../references/publish-manifest-format.md)。`resolve-luids` 完了済みが前提 (旧/新 PDS の LUID が必要) |
| `source_project` | design | 任意 | 棚卸し対象プロジェクト名 (既定 `0_Datasource`) |
| `output_dir` | 両方 | ✅ | 成果物出力先 (典型: `work/<yyyymmdd>_<tag>/reports/`) |
| `design_path` | verify | ✅ | design モードが出力した `repoint-design.json` のパス |

## 出力

`output_dir` 配下:

- design モード: **repoint-runbook.md** (人間向け) + **repoint-design.json** (verify 入力) +
  `repoint-inventory.json` (中間、デバッグ用に残す)
- verify モード: **repoint-verify-report.md**

メイン会話への戻り値の末尾に **`## Timing` ブロック** を必ず含める
([skill-timing-contract.md](../../../references/skill-timing-contract.md))。各スクリプトの
`RESULT_JSON:` 行に `elapsed_s` / `breakdown` があるので集約する。verify モードの戻り値には
**overall_verdict と未反映 WB の要約**も含める。

## ワークフロー — design モード

進捗:

- [ ] Step 1: inventory (Metadata API → 左辺 = WB × 旧 PDS)
- [ ] Step 2: build plan (manifest join → 右辺 = 旧 PDS → 新 PDS)
- [ ] Step 3: 戻り値要約

### Step 1: inventory

```bash
python ${CLAUDE_SKILL_DIR}/scripts/inventory_workbooks.py \
  --source-project <source_project (既定 0_Datasource)> \
  --manifest <manifest_path_1> --manifest <manifest_path_2> ... \
  --out <output_dir>/repoint-inventory.json
```

`source_project` 配下の PDS の `downstreamWorkbooks` を Metadata API (read-only) で走査し、対象 WB の
`webpage_url` と、manifest 記載の新 PDS の `content_url` を解決する。**デモ判定・利用状況フィルタは
しない** (design 決定 2 — 取捨は人間判断)。逆方向 `upstreamDatasources` は使わない (誤 FAIL 回避、
[lineage-model.md](references/lineage-model.md))。認証失効時は `signed_in_server()` がブラウザ
サインインを促すため、失敗したら caller にその旨を返す (fork 内で放置しない)。

### Step 2: build plan

```bash
python ${CLAUDE_SKILL_DIR}/scripts/build_repoint_plan.py \
  --inventory <output_dir>/repoint-inventory.json \
  --manifest <manifest_path_1> --manifest <manifest_path_2> ... \
  --out-dir <output_dir>
```

ローカル join のみ (サーバーアクセスなし)。旧 PDS luid ↔ manifest `original.outputs[].luid` を
主キーに `source_original_output_name` 経由で新 PDS を確定し、**repoint-design.json** と
**repoint-runbook.md** を 1 パスで生成する (両者は必ず一致)。旧 PDS の LUID が manifest に無ければ
PDS 名で fallback し `match: "name"` で明示する。対応先が確定できない旧 PDS は `unmapped_old_pds`
に落とす (join キーの詳細は [lineage-model.md](references/lineage-model.md))。

### Step 3: 戻り値要約

runbook のパス・対象 WB 数・ペア数・`unmapped_old_pds` 件数・warnings を要約して返す。次に人間が
Desktop で Replace Data Source を実施し、その後 verify を回す旨を添える。

## ワークフロー — verify モード

進捗:

- [ ] Step 1: 再走査で反映突合
- [ ] Step 2: 結果要約を caller に返す

```bash
python ${CLAUDE_SKILL_DIR}/scripts/verify_repoint.py \
  --design <design_path> --out <output_dir>/repoint-verify-report.md
```

design と **同じ `downstreamWorkbooks` クエリを 1 回** 実行し、design.json の各 (旧 PDS → 新 PDS, WB)
について「旧から消えた」「新に現れた」の両方で判定する。**逆方向クエリは使わない** (誤 FAIL 回避)。

**反映ラグ (eventual consistency) 対応 (必須)**: Metadata lineage は republish 後に反映ラグがある。
verify は**単一スナップショット**を取って報告するだけで、未反映は「時間をおいて再実行」を案内し、
**fail を自分では直さない**。数回再実行しても解消しない場合のみ、人間が Desktop をやり直すか caller が
design を再実行する ([lineage-model.md](references/lineage-model.md))。

戻り値には `overall_verdict` (`PASS` / `INCOMPLETE` / `EMPTY`) と `未反映 / 部分反映 WB の一覧` を含める。

## 失敗時の動作

スクリプトが失敗したら **その時点で停止し、caller にエラーを返す** (autonomous-recovery はしない。
読み取り専用なのでリトライ判断は caller)。よくある失敗:

- Metadata API が `errors` を返す / `old_pds` が空: 結果を「影響 WB なし」と誤読しない。project 名の誤り
  ・Metadata API 無効・認証失効を疑い caller に確認を返す (inventory は errors を必ず出力する)
- 旧/新 PDS の LUID が null: manifest の `resolve-luids` が未完了。`python scripts/publish_manifest.py
  resolve-luids --manifest ...` を先に実行するよう caller に返す (name fallback は動くが warning が出る)
- `unmapped_old_pds` が多い: manifest の渡し漏れの可能性。全移行セッションの manifest を列挙したか確認
- 認証失効: `python scripts/tableau_auth.py status` の確認と再サインイン (ユーザー在席) を caller に依頼

## 将来拡張

`.twb` XML 手術 (`<repository-location>` を新 PDS の content_url へ書換 → republish) で repoint を
自動化する write 経路は、design + verify が固まってから別途検討する (本番 BI 資産で blast radius が
大きい)。design.json に `content_url` を残すのはその布石。

## 依存

Python: `tableauserverclient` (repo の requirements.txt に含まれる)。サーバー触るスクリプト
(inventory / verify) は repo 共通の `scripts/tableau_auth.py` (`signed_in_server()`) を import する。
build_repoint_plan.py はローカル完結でサーバー不要。
