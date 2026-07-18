---
name: prep-workbook-repointer
description: 移行後、旧 Published Data Source を参照する Workbook を新 marts PDS へ差し替える Skill。design (Metadata API の downstreamWorkbooks と publish-manifest の突合で対象 WB・旧→新 PDS 対応を機械確定し repoint-runbook.md + repoint-design.json を生成) / repoint (TWB を DL して content_url と表示名を書き換え republish する自動差し替え。リハーサル publish → 証拠比較 → ユーザー承認 → 本番 Overwrite の段取りゲート必須) / verify (差し替え後にサーバー実測 lineage と突合) の 3 モード。「WB の参照置換」「workbook を新 PDS に差し替え」「repoint の設計資料を作って」「repoint を実行して」「自動で差し替えて」「参照置換を検証して」と言われたときに起動。差し替えの既定経路は repoint モードで、手術不可ケース・権限制約時のみ Desktop の Replace Data Source による人間差し替えに runbook で fallback する。サーバー書込は repoint モードの WB republish のみ (design / verify は読み取りのみ)。
context: fork
agent: general-purpose
allowed-tools: Read Write Bash(python *) Glob Grep
---

# prep-workbook-repointer

移行後、旧 PDS を参照する Workbook を新 marts PDS へ差し替える Skill。**設計** (design)・
**自動差し替え** (repoint)・**検証** (verify) の 3 モードを持つ。設計モデル・判断根拠は
[references/lineage-model.md](references/lineage-model.md)、TWB 手術の機構と制約は
[references/twb-surgery.md](references/twb-surgery.md)、出力形式は
[references/repoint-format.md](references/repoint-format.md)。

存在理由: 「どの WB を・どの接続を・どの新 PDS へ差し替えるか」を Metadata API lineage と
publish-manifest の突合で **機械確定** し、repoint モードが TWB 手術で自動実行する (既定経路)。
手術が fail-closed するケース (未対応のシリアライズ等、[twb-surgery.md](references/twb-surgery.md))
や自動 republish を許可しない運用では、人間が runbook を見て Desktop の Replace Data Source で
差し替える **fallback** に切り替える。いずれの経路でも差し替え後は同じ lineage を再走査して
verify する。

**サーバー書込は repoint モードの WB republish のみ**。本番 WB への Overwrite は blast radius が
大きいため、リハーサル → 証拠提示 → ユーザー明示承認 → 本番、の段取りゲートを必ず通す (後述)。

## Caller から渡される入力

fork は会話履歴を渡せないため起動時に以下を文章で明示する ([fork-skill-contract.md §1](../../../references/fork-skill-contract.md)):

| 入力 | モード | 必須 | 説明 |
|---|---|---|---|
| `mode` | — | ✅ | `design` / `repoint` / `verify` |
| `manifest_paths` | design | ✅ | 全移行セッションの publish-manifest JSON パスの**明示列挙** (glob しない — 別セッション残骸の混入防止)。複数フローは複数 work フォルダに分散するので caller が集約して渡す。形式は [publish-manifest-format.md](../../../references/publish-manifest-format.md)。`resolve-luids` 完了済みが前提 (旧/新 PDS の LUID が必要) |
| `source_project` | design | ✅ | 棚卸し対象プロジェクト名 (サイト固有。既定なし — caller が明示する) |
| `output_dir` | 全モード | ✅ | 成果物出力先 (典型: `work/<yyyymmdd>_<tag>/reports/`) |
| `design_path` | repoint / verify | ✅ | design モードが出力した `repoint-design.json` のパス |
| `stage` | repoint | ✅ | `rehearsal` または `production`。**production はユーザーが rehearsal の証拠を見て明示承認した後にのみ渡される** (caller が保証する) |
| `target_workbooks` | repoint | ✅ | 差し替える WB LUID の列挙、または「全件」の明示 |
| `rehearsal_project` | repoint (rehearsal) | ✅ | リハーサル copy の publish 先プロジェクト名 / LUID (使い捨て置き場。例: sandbox 系プロジェクト) |
| `work_dir` | repoint | ✅ | DL した TWB / 手術済み TWB の置き場 (典型: `work/<yyyymmdd>_<tag>/scratch/`) |

## 出力

`output_dir` 配下:

- design モード: **repoint-runbook.md** (人間向け) + **repoint-design.json** (repoint / verify 入力) +
  `repoint-inventory.json` (中間、デバッグ用に残す)
- repoint モード: `RESULT_JSON` の per-WB 結果 (publish 先 LUID・置換カウント・接続チェック)。
  rehearsal 時はさらに **repoint-rehearsal-report.html / .md** (承認ゲートの 1 枚レポート、
  同一データから 1 パス生成) + **view-compare.html / .json** (その証拠実体)。手術済み .twb は
  `work_dir` に監査用残置
- verify モード: **repoint-verify-report.md**

戻り値末尾の `## Timing` ブロックと verify モードの verdict 要約 (overall_verdict と未反映 WB) は
[fork-skill-contract.md §2](../../../references/fork-skill-contract.md) に従う。各スクリプトの
`RESULT_JSON:` 行に `elapsed_s` / `breakdown` があるので集約する。

## ワークフロー — design モード

進捗:

- [ ] Step 1: inventory (Metadata API → 左辺 = WB × 旧 PDS)
- [ ] Step 2: build plan (manifest join → 右辺 = 旧 PDS → 新 PDS)
- [ ] Step 3: 戻り値要約

### Step 1: inventory

```bash
python ${CLAUDE_SKILL_DIR}/scripts/inventory_workbooks.py \
  --source-project <source_project> \
  --manifest <manifest_path_1> --manifest <manifest_path_2> ... \
  --out <output_dir>/repoint-inventory.json
```

`source_project` 配下の PDS の `downstreamWorkbooks` を Metadata API (read-only) で走査し、対象 WB の
`webpage_url` と、manifest 記載の新 PDS の `content_url` を解決する。**デモ判定・利用状況フィルタは
しない** (取捨は人間判断)。逆方向 `upstreamDatasources` は使わない (誤 FAIL 回避、
[lineage-model.md](references/lineage-model.md))。認証失効の扱いは
[fork-skill-contract.md §4](../../../references/fork-skill-contract.md)。

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

runbook のパス・対象 WB 数・ペア数・`unmapped_old_pds` 件数・warnings を要約して返す。次は
repoint モード (rehearsal から) が既定で、Desktop 作業は fallback である旨を添える。

## ワークフロー — repoint モード

**rehearsal と production は別 invocation** — fork 内ではユーザーと対話できないため、承認ゲートは
invocation の間 (メイン会話) に置く。caller は rehearsal の証拠をユーザーに提示し、明示承認を得て
から production で再起動する。手術の機構・publish 契約・制約は
[twb-surgery.md](references/twb-surgery.md)。

### stage = rehearsal

進捗:

- [ ] Step 1: 手術 + リハーサル publish
- [ ] Step 2: 証拠取得 (view 比較、WB ごと)
- [ ] Step 3: 承認レポート生成
- [ ] Step 4: レポート要約を caller に返す

```bash
python ${CLAUDE_SKILL_DIR}/scripts/repoint_workbook.py \
  --design <design_path> \
  --workbook <wb_luid> [--workbook <wb_luid> ...] \
  --stage rehearsal --rehearsal-project <rehearsal_project> \
  --work-dir <work_dir> \
  --result-out <output_dir>/repoint-rehearsal-result.json
```

WB を DL → content_url + 表示名の全文置換 → リハーサル用プロジェクトへ別名 publish (元 WB は
無傷)。接続チェック (旧 PDS 名の残存ゼロ・新 PDS 名の出現) まで自動で行う。続けて **WB ごとに**
元 WB × copy の view 別比較で証拠を取る:

```bash
python ${CLAUDE_SKILL_DIR}/scripts/compare_workbook_views.py \
  --baseline <元WB luid> --candidate <リハーサルcopy luid> \
  --label-baseline original --label-candidate repointed \
  --out-dir <output_dir>/repoint-rehearsal-<wb名slug>
```

最後に両者の機械出力を join して承認ゲート用の 1 枚レポートを生成する:

```bash
python ${CLAUDE_SKILL_DIR}/scripts/render_rehearsal_report.py \
  --repoint-result <output_dir>/repoint-rehearsal-result.json \
  --compare <output_dir>/repoint-rehearsal-<wb名slug>/view-compare.json [...] \
  --out <output_dir>/repoint-rehearsal-report.md
```

レポートは .md と **.html** (同 stem) の 2 形式で生成される。機械判定 (`READY_FOR_APPROVAL` /
`NOT_READY`) は**接続切替 + copy 側の全 view export 成功のみ**を保証し、行数・画像並置 (埋め込み) は
人間の目視確認材料とする。**データ同値性はこのゲートで再判定しない** — 旧 vs 新 PDS の parity は
repoint の事前条件として prep-output-comparator で検証済みが前提 ([twb-surgery.md](references/twb-surgery.md))。
戻り値には機械判定・要確認件数・**HTML レポートのパス**を含め、**production はユーザーがこの
レポートを確認して明示承認した後にのみ起動される**旨を添える。caller は受け取った HTML をブラウザで
開いてユーザーに提示する (Stop 2 の .html 視覚レビューと同じ運用 — ワンクリックで承認判断できる
状態にする)。

### stage = production

進捗:

- [ ] Step 1: 本番 Overwrite publish
- [ ] Step 2: 結果要約を caller に返す

```bash
python ${CLAUDE_SKILL_DIR}/scripts/repoint_workbook.py \
  --design <design_path> \
  --workbook <wb_luid> [--workbook <wb_luid> ...] \
  --stage production --work-dir <work_dir>
```

元 WB と**同名・同プロジェクト**に Overwrite publish するため WB LUID と webpage URL は不変
(権限・埋め込み URL が生き残る)。`show_tabs` は publish 前に実値を取得して再指定される。完了後、
caller に verify モードの実行を促す。リハーサル copy の削除は人間判断 (本 Skill は消さない)。

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
**fail を自分では直さない**。数回再実行しても解消しない場合のみ、差し替え (repoint モードまたは
Desktop) をやり直すか caller が design を再実行する ([lineage-model.md](references/lineage-model.md))。

戻り値には `overall_verdict` (`PASS` / `INCOMPLETE` / `EMPTY`) と `未反映 / 部分反映 WB の一覧` を含める。

## 失敗時の動作

失敗時は停止して caller にエラーを返す ([fork-skill-contract.md §3](../../../references/fork-skill-contract.md)、認証失効は §4)。本 Skill 固有のよくある失敗:

- Metadata API が `errors` を返す / `old_pds` が空: 結果を「影響 WB なし」と誤読しない。project 名の誤り
  ・Metadata API 無効・認証失効を疑い caller に確認を返す (inventory は errors を必ず出力する)
- 旧/新 PDS の LUID が null: manifest の `resolve-luids` が未完了。`python scripts/publish_manifest.py
  resolve-luids --manifest ...` を先に実行するよう caller に返す (design の name fallback は動くが、
  **repoint モードは LUID null のペアを手術できない**)
- `unmapped_old_pds` が多い: manifest の渡し漏れの可能性。全移行セッションの manifest を列挙したか確認
- repoint で `no reference to old PDS ... found in TWB`: design が stale (WB が既に差し替え済み /
  別 PDS 参照)。design を再実行してから repoint し直す。なお WB が旧 content_url (再 publish 前の
  値) を参照しているだけなら手術は TWB 実トークンで自動追従し warning を出す
  ([twb-surgery.md](references/twb-surgery.md))
- repoint の接続チェック FAIL (`stale_old_names` / `missing_new_names`): 手術不良。publish 済みの
  リハーサル copy はそのまま証拠として残し、caller にエラー内容を返す (本番 stage はこの状態で
  実行しない)

## 依存

Python: `tableauserverclient` (repo の requirements.txt に含まれる)。サーバーを触るスクリプト
(inventory / repoint / compare / verify) は認証に repo 共通の `scripts/tableau_auth.py` を import する
([fork-skill-contract.md §4](../../../references/fork-skill-contract.md))。
build_repoint_plan.py はローカル完結でサーバー不要。
