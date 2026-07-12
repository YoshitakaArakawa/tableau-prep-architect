---
name: prep-architect
description: prep-extractor が生成した flow-summary.md を入力に、Tableau Prep の長大フローを dbt 流のレイヤ規律で分析・分解設計する。analyze（現状把握）と decompose（分解設計）の 2 フェーズを、ユーザー指示に応じて順次または個別に実行する。既存の .tfl/.tflx を「分析したい」「分解したい」「dbt 風に再構築したい」「最適化したい」と言われたときに起動。実装（.tfl 生成）は prep-builder、publish 以降は prep-deployer が担当。
context: fork
agent: general-purpose
model: sonnet
allowed-tools: Read Write Edit Glob Grep Bash(python *)
---

# prep-architect

Tableau Prep のフローを dbt 流のレイヤ規律（stg / intermediate / marts）で **解釈** し、**分解設計** する Skill。実装（.tfl 生成）は [prep-builder](../prep-builder/SKILL.md)、publish 以降は [prep-deployer](../prep-deployer/SKILL.md) の責務。

## 前提

本 Skill は **prep-extractor が先に走って flow-summary.md を生成済みであること** を前提に動く:

- 大きな flow.json をメイン会話のコンテキストに読み込ませない
- 構造抽出は [prep-extractor](../prep-extractor/SKILL.md) が責任を持つ
- 本 Skill が **Read するのは flow-summary.md 等のサマリのみ**。元 .tfl / flow.json は decompose のスクリプト (gen_plan_skeleton.py / render_plan_md.py) にパスとして渡すだけで、本文をコンテキストに読み込まない

ユーザーが分析を要求した時点で flow-summary.md が無ければ、まず prep-extractor の起動を案内する。

## Caller から渡される入力

`context: fork` で動くため caller (メインエージェント) は会話履歴を渡せない。起動時に以下を文章で明示すること:

| 入力 | 必須 | 例 |
|---|---|---|
| `mode` | ✅ | `analyze` / `decompose` / `both` |
| `flow_summary_path` | ✅ | prep-extractor が出力した `flow-summary.md` のパス |
| `deploy_context_path` | decompose で推奨 | prep-extractor Phase B の `deploy-context.md`。既存 flow 名衝突回避 + passthrough Input の PDS LUID 解決に使う |
| `input_dispatch_mech_path` | decompose で **必須** | prep-extractor Phase B の `input-dispatch-mech.json`。各 Input の kind / fields / 解決済 LUID を読み、policy 提案 + stg rename (元名ピン留め) + provisioning 案を decompose 内で組み立てる |
| `analysis_path` | 任意 | analyze を実施済みなら `analysis-<flow>.md` のパス。**decompose 単独 (analyze 未実施) では不要** — `flow-summary.md` + `input-dispatch-mech.json` だけで decompose は成立する |
| `flow_dependencies_path` | 任意 (複数フロー移行時に推奨) | prep-extractor Phase C の `flow-dependencies.md`。pds 入力の出所分類 (in-scope 出力 = 暫定 passthrough) と stg 再利用判断 (self-check 項目 15) に使う。無ければ deploy-context から推定し `未確認` ラベル |
| `output_dir` | ✅ | `analysis-<flow>.md` / `decomposition-plan-<flow>.json` + `.md` の出力先 (典型: `work/<yyyymmdd>_<tag>/reports/`)。レポート類は [CLAUDE.md §work/ ディレクトリ規約](../../../CLAUDE.md#work-ディレクトリ規約) の `reports/` に集約 |
| `source_tfl_path` | decompose で必須 | 元 .tfl/.tflx (または展開済み flow.json)。decompose のスクリプト入力 (本文は読まない) |

## Phases

### Analyze（現状把握）

flow-summary.md の構造情報に **業務的解釈** を加えて分析レポート（`analysis-<flow-name>.md`）を出す。

| 項目 | 内容 |
|---|---|
| 入力 | `flow-summary.md`（prep-extractor の出力） |
| 出力 | `analysis-<flow-name>.md`（書式: [references/analysis-report-format.md](references/analysis-report-format.md)）|
| 主な判断 | レイヤ推定（stg / int / mart）、Input Compliance、分解境界候補 |
| 主な参照 | [../../../references/layer-responsibilities.md](../../../references/layer-responsibilities.md), [../../../references/input-policy.md](../../../references/input-policy.md) |

### Decompose（分解設計）

analyze の結果と `input-dispatch-mech.json` を基に、分解設計を **`decomposition-plan-<flow>.json`** (機械可読、[../../../references/plan-json-schema.md](../../../references/plan-json-schema.md)) として出す。レビュー用の `decomposition-plan-<flow>.md` (git 追跡の設計記録) と `.html` (Stop 2 の視覚レビュー面) は **スクリプトが plan.json から同一検証パスでレンダリングする** — 手書きしない。

| 項目 | 内容 |
|---|---|
| 入力 | `flow-summary.md`, `analysis-<flow-name>.md`, `input-dispatch-mech.json`, `deploy-context.md`, 元 .tfl/.tflx (スクリプト入力としてのみ — 本文は読まない) |
| 出力 | `decomposition-plan-<flow>.json` (設計の正) + `decomposition-plan-<flow>.md` / `.html` (レンダリング産物、書式: [../../../references/decomposition-plan-format.md](../../../references/decomposition-plan-format.md)) |
| 主な判断 | レイヤ分割、actions レベル分割、移行順序、**stg policy + Materialization** (mech findings から導出)、**stg rename の元名ピン留め** (semantic translation は禁止 — [../../../references/input-policy.md §命名レジーム](../../../references/input-policy.md))、**mart 列名 parity** (命名レジーム下で自動達成、divergent rename 導入時のみ Rename-back) |
| 主な参照 | [../../../references/layer-responsibilities.md](../../../references/layer-responsibilities.md), [references/intermediate-decomposition.md](references/intermediate-decomposition.md), [references/review-checkpoints.md](references/review-checkpoints.md), [../../../references/naming-conventions.md](../../../references/naming-conventions.md), [../../../references/input-policy.md](../../../references/input-policy.md), [../../../references/project-hierarchy.md](../../../references/project-hierarchy.md) |

手順 (LUID / path / transforms 表の手書きは廃止 — 機械部分はスクリプトが埋める):

1. **skeleton 生成**: `python ${CLAUDE_SKILL_DIR}/scripts/gen_plan_skeleton.py --source <元.tfl> --input-dispatch <input-dispatch-mech.json> --deploy-context <deploy-context.md> --out <output_dir>/decomposition-plan-<flow>.json` — server / プロジェクト LUID / 元 outputs / vconn stg entry (transforms 事前充填) / passthrough hint / needs_provisioning entry が埋まった状態で出る
2. **設計フィールドを記入** (Edit): int / marts の entry (`included_steps` / `splits` / `inputs` / `rename_back` / `joins` / `description` / `source_original_output_name`)、stg の `to_caption` は skeleton 初期値 (現行 caption) を維持し、actions-split で吸収した正規化のみ上書き。step 番号は flow-summary の Topology 表と同一。書き終えたら `_` 始まりのキーを全削除
3. **self-check**: [references/decompose-self-check.md](references/decompose-self-check.md) を Read して全項目を通す (lineage 到達性・step 範囲・配線可能性は次の render が機械検証するので、self-check は業務判断系の項目に集中する)
4. **検証 + レンダリング**: `python ${CLAUDE_SKILL_DIR}/scripts/render_plan_md.py --plan <plan.json> --source <元.tfl> -o <output_dir>/decomposition-plan-<flow>.md` — md と同じディレクトリに `.html` (Stop 2 の視覚レビュー面) も同時に出る。検証エラーが出たら plan.json を修正して再実行 (エラーのまま md を手書きで補わない)

**`input-dispatch-mech.json` の各 Input record から plan を組み立てるルール** は [../../../references/decomposition-plan-format.md §Input dispatch と stg materialization](../../../references/decomposition-plan-format.md) を正典として従う (kind → policy 対応の要点: `pds` resolved → `passthrough` / `pds` ambiguous・unresolved → `augment` 暫定 + Stop 2 で disambiguate / `vconn` → `augment` + `Materialization: live_pds` + rename ピン留め / `direct_db`・`extract` → `needs_provisioning` + provisioning 案)。`unknown` は extractor 側で raise されるため本 Skill 起動時には存在しない前提。record の JSON スキーマは [../prep-extractor/references/input-dispatch-format.md](../prep-extractor/references/input-dispatch-format.md)。

## How to invoke

ユーザー指示の解釈:

| 指示 | 動作 |
|---|---|
| 「分析して」「現状把握したい」 | analyze のみ実行 |
| 「分解設計して」「.tfl 構成を考えて」 | analyze 未実施なら analyze → decompose、実施済みなら decompose のみ |
| 「再構築したい」「dbt 風に整理して」 | analyze → decompose を順次実行、完了後 prep-builder の起動を案内 |
| flow-summary.md が無い | prep-extractor の起動を案内、本 Skill は中断 |
| 「特定のステップだけ」「intermediate だけ深掘り」 | 該当部分の analyze / decompose に絞る |

**確認関所は decompose 完了後の 1 箇所のみ**。analyze → decompose の間で止まらず一気通貫で進む。decompose 完了時に `decomposition-plan-<flow>.md` + `.html` を出力したら、build（prep-builder）に進む前にユーザー確認 (Stop 2) を 1 回取る (caller は `.html` のパスを提示してブラウザで開いてもらう)。確認観点は [references/review-checkpoints.md](references/review-checkpoints.md) に集約 (Tier 1 明示確認 / Tier 2 デフォルト受諾 / Tier 3 Agent 自律)。

### decompose 完了前の self-check (必須)

render_plan_md.py を流す前に、[references/decompose-self-check.md](references/decompose-self-check.md) を **Read して全項目のチェックを必ず通し、不整合があれば是正してから出力する** (項目一覧・判定基準・典型 anti-pattern は同ファイルが正典)。lineage 到達性・配線可能性・step 範囲は render_plan_md.py が機械検証し、build 時にも `verify_lineage_closure` / `verify_edge_namespaces` が再チェックする (三重防御) — self-check は機械化できない業務判断系の項目に集中する。

## 出力契約

本 Skill は `context: fork` で動くため、生成物は **必ず `output_dir` 配下にファイル** として出力する。フローの長短にかかわらず inline 返しはしない。

| mode | 出力ファイル |
|---|---|
| `analyze` | `<output_dir>/analysis-<flow>.md` |
| `decompose` | `<output_dir>/decomposition-plan-<flow>.json` + `.md` + `.html` (render_plan_md.py 産物) |
| `both` | 上記両方 |

メイン会話への戻り値は **実行サマリのみ**: 書いたファイルパス・主要な判断 (推定レイヤ構成・分解候補数)・self-check / render 検証の異常点。生成本文の全文を戻り値に詰めない (fork した意義 = メイン context の保護が損なわれ、後段 prep-builder はこれらをファイル参照前提で読むため契約破壊になる)。

**出力量は最小化する** — write token が fork wall clock の支配要因。LLM が書くのは analysis の付加価値列と plan.json の設計フィールドのみで、定型部 (LUID / path / transforms 表 / lineage 表 / DAG / md 全文) はスクリプトが生成する。`description` は 1-2 行、`alternatives` は非自明な分岐がない限り省略。

戻り値末尾に **`## Timing` ブロック** を必ず含める (フォーマットと Skill 別 breakdown 推奨項目: [skill-timing-contract.md](../../../references/skill-timing-contract.md))。

Write が失敗した場合は inline fallback せず、その時点で停止して caller にエラーを返す (どのファイルへの書き込みが失敗したか・原因を含む)。

## prep-builder への引き渡し

decompose 完了後、prep-builder に渡すのは:

1. `decomposition-plan-<flow-name>.json`（設計の真の source — md はレビュー用のレンダリング産物）
2. 元 .tfl / .tflx（build 時の元ノード定義の参照元）

prep-builder は `build_from_plan.py` でこの 2 つから新 .tfl 群を機械生成し、続いて prep-deployer が publish / run を行う。

## 設計原則

- 元 .tfl は本 Skill では絶対に変更しない（build は prep-builder の責務）
- 設計フェーズと実装フェーズを分ける（decompose 完了で必ず一度止まる）
- 未知のノード種別・循環依存等は中断してユーザーに報告
- git をソース・オブ・トゥルースに
