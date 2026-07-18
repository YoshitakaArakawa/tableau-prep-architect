---
name: tableau-pds-backfiller
description: 分解後の incremental accumulator PDS に、旧 output PDS の累積履歴を hyper-level surgery で一度だけ seed する (backfill) Skill。deployed flow から accumulator を解決し、列整合検証 → schedule interlock → snapshot 退避 → dry-run → sandbox preview → 明示承認後の本番 Overwrite → 受け入れ incremental 1 サイクル → schedule 再開 を段階実行する。seam (baseline より前の履歴のみ挿入) と replace (sentinel を捨てて全ロード) の 2 モードを持つ。「backfill して」「旧 PDS の履歴を新 PDS に移して」「履歴を seed して」と言われたとき、incremental フロー分解後に過去履歴が欠けているときに起動。値比較・parity 判定は持たない (tableau-pds-comparator に委譲)。移行セッション冒頭の intake・goal ゲート・起動順序は references/migration-workflow.md が正典（本 Skill 単体で移行セッションを始めない）。
---

# tableau-pds-backfiller

incremental Prep フローを dbt 流に分解すると、新しい **incremental accumulator PDS は現ソースの baseline (最新バッチ) しか持たない**。過去の累積履歴は旧 output PDS にのみ存在し、現ソースからは再構築できない (incremental フローは最新バッチしか読まないため)。

**Backfill = 旧 PDS の履歴を新 accumulator に一度だけ seed し、以後は前方 incremental で継続する工程**。本 Skill は旧・新 PDS を .tdsx (extract 込み) で download し、`.hyper` を name-align で結合して旧の履歴行を新抽出へ挿入し、Overwrite publish する。取り消しにくい本番書き込みを含むため、**dry-run → sandbox preview → 明示承認 → 本番 → 受け入れ** の段取りそのものが安全要件。

本 Skill は `context: fork` を **付けない**。本番 Overwrite の失敗観測と、ユーザー判断 2 ゲート (seam/replace 選択・本番承認) を主会話で扱うため (tableau-prep-deployer と同じ整理)。

値比較・parity 判定は持たず、事後 parity は [tableau-pds-comparator](../tableau-pds-comparator/SKILL.md) へ橋渡しする。

## スコープ

含む:
- deployed flow から incremental+append accumulator を出力単位で解決 (`resolve_accumulator.py`)
- 旧・新 PDS の列整合検証 (`diff_pds_schema.py`、rename / cast 対応付け)
- seam モード (baseline `MIN(control)` より前の旧行のみ挿入、二重化なし) / replace モード (sentinel を捨てて旧を全ロード)
- snapshot 自動退避 + `--restore` による即時ロールバック
- dry-run (ローカル挿入 + 行数検算、本番未書込) → sandbox preview → 本番 Overwrite → サーバー再検証
- audit manifest (`backfill-manifest.json`) への全実行記録

含まない:
- **schedule の suspend / resume** — Cloud の Linked Task は REST で変更できない (UI 専用)。本 Skill は schedule を **時刻ベースで検査**し (`check_flow_readiness.py`)、次回 run が操作 window 内に迫る schedule だけを blocker として UI 手動 suspend を案内する (window 外は advisory、suspend 不要)
- **下流 mart / cross-flow consumer の再 run** — [tableau-prep-deployer](../tableau-prep-deployer/SKILL.md) の `run_flow.py` (FULL) に委譲 (Step 7)。**受け入れ incremental 1 サイクル (Step 6) は本 Skill が担う** — tableau-prep-deployer の `run_flow.py --incremental` を直接呼んで実走し、二重化ゼロ・正しい追記を確認する (backfill 継続性の実証なので backfill 工程の一部)
- **値同値・parity 判定** — [tableau-pds-comparator](../tableau-pds-comparator/SKILL.md) に委譲 (挿入行は逐語コピーなので値同値は構造保証、行数 parity のみ確認すれば足りる)
- seam / replace の自動選択 — baseline を提示してユーザーに選ばせる

## 副作用と承認

| 段階 | 副作用 | 承認 |
|---|---|---|
| resolve / diff / readiness check | Cloud 読み取りのみ | 不要 |
| snapshot 退避 / dry-run / rezip | ローカルのみ (本番未書込) | 不要 |
| sandbox preview publish | 使い捨て project に別名 PDS 追加 (本番不変) | 不要 (非破壊) |
| 本番 Overwrite (`--execute`) | accumulator PDS の破壊的更新 | **ユーザーゲート②: 明示承認必須** |
| restore (`--restore`) | snapshot を Overwrite し直す (ロールバック) | 失敗時の一次手段、承認済み前提 |

## ライフサイクル

進捗 (詳細は [references/backfill-recipe.md](references/backfill-recipe.md)):

- [ ] Step 0: 前提条件チェック — refuse 条件に該当なら中止 ([preconditions](references/preconditions-and-edge-cases.md))
- [ ] Step 1: schedule interlock — in-flight run 無し + 次回 run が操作 window 外を確認 (window 内なら UI で suspend)
- [ ] Step 2: 対象・列・seam 確定 — **★ゲート① seam か replace か**
- [ ] Step 3: dry-run — ローカル挿入 + 行数検算
- [ ] Step 4: sandbox preview — 使い捨て project へ別名 publish、GUI 確認
- [ ] Step 5: 本番 Overwrite — **★ゲート② 明示承認** (snapshot 自動退避済み)
- [ ] Step 6: 事後検証 + 受け入れ incremental 1 サイクル
- [ ] Step 7: 下流再 run (tableau-prep-deployer に委譲)
- [ ] Step 8: schedule 再開 (UI)

失敗時は `python ${CLAUDE_SKILL_DIR}/scripts/backfill_pds.py --spec <spec> --restore <tag>` で snapshot から即時ロールバック。

## ユーザー判断 2 ゲート

1. **seam か replace か** (Step 2): baseline の行数・control の MIN/MAX/distinct を提示して選ばせる。sentinel (実データ範囲外の far-past 値だけ) なら replace、正しい baseline なら seam。自動判定しない
2. **本番 Overwrite の明示承認** (Step 5): 取り消しにくい書き込み。dry-run と preview の結果を提示し、ユーザーの明示 OK を得てから `--execute`

## How to invoke

| 指示 | 動作 |
|---|---|
| 「backfill して」「履歴を seed して」 | Step 0 から順に。まず `resolve_accumulator.py` で対象確定 |
| 「旧 PDS の履歴を新 accumulator に移して」 | 同上。old_luid / new_luid が明示されていれば Step 2 の diff から |
| 「backfill を dry-run して」 | Step 3 まで (本番未書込)。preview まで進めるか確認 |
| 「backfill をロールバックして」 | `backfill_pds.py --restore <tag>` |
| 「accumulator を backfill できるか確認して」 | `resolve_accumulator.py` + `check_flow_readiness.py` で可否だけ提示 |

spec (`old_luid` / `new_luid` / `control` / `mode` / `column_map`) をユーザーが提示しない場合は resolve / diff で組み立てるが、**seam/replace と本番承認はユーザーに必ず聞く** (ゲート①②)。

## 認証

OAuth 2.0 (Authorization Code + PKCE) のブラウザサインイン。詳細は [tableau-prep-deployer/references/authentication.md](../tableau-prep-deployer/references/authentication.md)。

## 依存

Python: `tableauserverclient` / `tableauhyperapi` / `python-dotenv` (repo の [requirements.txt](../../../requirements.txt))。`tableauhyperapi` は本 Skill 固有の依存 (.hyper 手術に使う)。

## Scripts

| スクリプト | 役割 |
|---|---|
| `scripts/resolve_accumulator.py` | deployed flow から incremental+append accumulator を出力単位で解決、PDS LUID まで resolve (読み取りのみ) |
| `scripts/diff_pds_schema.py` | 旧・新 PDS のメタデータ JSON を純粋 diff、rename の pre-map / 型不一致検出 (auth 不要) |
| `scripts/backfill_pds.py` | download → snapshot 退避 → attach + INSERT-SELECT (seam/replace) → 行数検算 → rezip → `--execute` で Overwrite → サーバー再検証。`--restore <tag>` でロールバック。manifest 追記、RESULT_JSON emit |
| `scripts/publish_preview.py` | seed 済み .tdsx を使い捨て project へ別名 publish (GUI 確認用、本番不変) |
| `scripts/check_flow_readiness.py` | accumulator flow の Active schedule + in-flight run を検査 (読み取りのみ、suspend は UI 手動) |

スクリプトは単独で動く (Skill 経由でも `python ${CLAUDE_SKILL_DIR}/scripts/<name>.py ...` で直接呼んでも同じ)。失敗は握り潰さない (HTTP status / 検証結果を caller に返す)。設計原則は §スコープ・§副作用と承認・§ユーザー判断 2 ゲート に集約済み。
