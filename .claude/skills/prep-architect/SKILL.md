---
name: prep-architect
description: prep-extractor が生成した flow-summary.md を入力に、Tableau Prep の長大フローを dbt 流のレイヤ規律で分析・分解設計する。analyze（現状把握）と decompose（分解設計）の 2 フェーズを、ユーザー指示に応じて順次または個別に実行する。既存の .tfl/.tflx を「分析したい」「分解したい」「dbt 風に再構築したい」「最適化したい」と言われたときに起動。実装（.tfl 生成）は prep-builder、publish 以降は prep-deployer が担当。
context: fork
agent: claude
model: claude-sonnet-4-6
allowed-tools: Read Write Glob Grep
---

# prep-architect

Tableau Prep のフローを dbt 流のレイヤ規律（stg / intermediate / marts）で **解釈** し、**分解設計** する Skill。実装（.tfl 生成）は [prep-builder](../prep-builder/SKILL.md)、publish 以降は [prep-deployer](../prep-deployer/SKILL.md) の責務。

## 前提

本 Skill は **prep-extractor が先に走って flow-summary.md を生成済みであること** を前提に動く:

- 大きな flow.json をメイン会話のコンテキストに読み込ませない
- 構造抽出は [prep-extractor](../prep-extractor/SKILL.md) が責任を持つ
- 本 Skill の analyze / decompose は **flow-summary.md のみを入力** とする

ユーザーが分析を要求した時点で flow-summary.md が無ければ、まず prep-extractor の起動を案内する。

## Caller から渡される入力

`context: fork` で動くため caller (メインエージェント) は会話履歴を渡せない。起動時に以下を文章で明示すること:

| 入力 | 必須 | 例 |
|---|---|---|
| `mode` | ✅ | `analyze` / `decompose` / `both` |
| `flow_summary_path` | ✅ | prep-extractor が出力した `flow-summary.md` のパス |
| `deploy_context_path` | decompose で推奨 | prep-extractor Phase B の `deploy-context.md`。既存 flow 名衝突回避 + passthrough Input の PDS LUID 解決に使う |
| `input_dispatch_mech_path` | decompose で **必須** | prep-extractor Phase B の `input-dispatch-mech.json`。各 Input の kind / fields / 解決済 LUID を読み、policy 提案 + rename 翻訳 + provisioning 案を decompose 内で組み立てる |
| `analysis_path` | decompose のみで使うなら | analyze 結果がある場合のみ |
| `output_dir` | ✅ | `analysis-<flow>.md` / `decomposition-plan-<flow>.md` の出力先 (典型: `work/<yyyymmdd>_<tag>/reports/`)。MD レポートは [CLAUDE.md §work/ ディレクトリ規約](../../../CLAUDE.md#work-ディレクトリ規約) の `reports/` に集約 |

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

analyze の結果と `input-dispatch-mech.json` を基に、新 .tfl の構成・命名・配置・migration order を設計した `decomposition-plan-<flow-name>.md` を出す。

| 項目 | 内容 |
|---|---|
| 入力 | `flow-summary.md`, `analysis-<flow-name>.md`, `input-dispatch-mech.json` |
| 出力 | `decomposition-plan-<flow-name>.md`（書式: [../../../references/decomposition-plan-format.md](../../../references/decomposition-plan-format.md)）|
| 主な判断 | レイヤ分割、actions レベル分割、サブプロジェクト配置、移行順序、**stg policy + Materialization** (mech findings から導出)、**Input rename 翻訳** (非 ASCII caption の semantic translation を含む)、**mart Rename-back 表** (元 output を引き継ぐ mart は、自分が導入した順方向 rename の逆写像をサフィックス保存で適用し、出力列名を元 output PDS と完全一致させる — [../../../references/decomposition-plan-format.md §Rename-back](../../../references/decomposition-plan-format.md)) |
| 主な参照 | [../../../references/layer-responsibilities.md](../../../references/layer-responsibilities.md), [references/intermediate-decomposition.md](references/intermediate-decomposition.md), [references/review-checkpoints.md](references/review-checkpoints.md), [../../../references/naming-conventions.md](../../../references/naming-conventions.md), [../../../references/input-policy.md](../../../references/input-policy.md), [../../../references/project-hierarchy.md](../../../references/project-hierarchy.md) |

**`input-dispatch-mech.json` の各 Input record から plan を組み立てるルール**:

| kind + 状態 | plan 内の扱い | policy |
|---|---|---|
| `pds` + `resolution.status=resolved` | stg entry を **作らない**。元 PDS を消費する下流 entry の `Inputs` に直書き (project_path + name + LUID を record から転記) | `passthrough` |
| `pds` + `resolution.status=ambiguous` または `unresolved` | Stop 2 でユーザー disambiguate / Phase B 再 scan を求める。デフォルト提案は `augment` (新規 stg を build して PDS 不在を回避) | `augment` (暫定) |
| `vconn` | stg entry を作り `Materialization: live_pds`、record の `fields[]` + `augmenter_columns_hint` から Rename proposals 表を組み立て (非 ASCII caption は semantic translation 提案、ASCII は snake_case 化) | `augment` |
| `direct_db` | stg entry を作るが `input_status: needs_provisioning`、`## Input provisioning required` セクションに vconn 化 / PDS 化案を 1 件追加 | `needs_provisioning` |
| `extract` | 同上 (extract → PDS publish 案) | `needs_provisioning` |

`unknown` は extractor 側で raise されているため、本 Skill 起動時には存在しない前提。

各 Input の `kind` 別の意味と JSON スキーマは [../../prep-extractor/references/input-dispatch-format.md](../../prep-extractor/references/input-dispatch-format.md)。

## How to invoke

ユーザー指示の解釈:

| 指示 | 動作 |
|---|---|
| 「分析して」「現状把握したい」 | analyze のみ実行 |
| 「分解設計して」「.tfl 構成を考えて」 | analyze 未実施なら analyze → decompose、実施済みなら decompose のみ |
| 「再構築したい」「dbt 風に整理して」 | analyze → decompose を順次実行、完了後 prep-builder の起動を案内 |
| flow-summary.md が無い | prep-extractor の起動を案内、本 Skill は中断 |
| 「特定のステップだけ」「intermediate だけ深掘り」 | 該当部分の analyze / decompose に絞る |

**確認関所は decompose 完了後の 1 箇所のみ**。analyze → decompose の間で止まらず一気通貫で進む。decompose 完了時に `decomposition-plan-<flow>.md` を出力したら、build（prep-builder）に進む前にユーザー確認 (Stop 2) を 1 回取る。確認観点は [references/review-checkpoints.md](references/review-checkpoints.md) に集約 (Tier 1 明示確認 / Tier 2 デフォルト受諾 / Tier 3 Agent 自律)。

### decompose 完了前の self-check (必須)

`decomposition-plan-<flow>.md` を出力する前に **必ず 15 項目チェック** を通す。詳細な判定基準と典型 anti-pattern は [references/decompose-self-check.md](references/decompose-self-check.md) を Read で取得。要約:

1. Upstream lineage 表を各 .tfl で埋める
2. Prev 連鎖が Inputs に到達することを確認 (逆推定禁止)
3. SuperUnion を削除候補に含めない (`Table Names` 暗黙列依存)
4. `## Output mapping` セクションを埋める (publish_manifest の必須入力)
5. 型変換 / 名前変換を staging に集中
6. 同一ソースの Input は staging で 1 回に集約
7. Dependency DAG に Before / After 2 ブロック
8. Join を含む .tfl に Joins cardinality を明示 (不明なら `不明`)
9. marts AddCol が intermediate に下げられないか検討
10. marts Filter が上流に下げられないか検討
11. 列削除 actions の元順序保全 (削除と参照の前後関係を逆転させない)
12. 分岐ノード下流の列要件チェック (列セット divergence なら intermediate 分割)
13. `Materialization=live_pds` の stg Transforms 表に `rename` / `cast` / `hide` 以外の op が混じっていない (row-level 操作は augmenter で表現不可、混在を検出したら当該 actions を int 側に分割)
14. 元 output を引き継ぐ mart に Rename-back 表があり、その mart に到達する rename 済み列を全カバーしている (内部名の露出ゼロ)
15. 1〜14 で不整合があれば是正してから出力

省略すると prep-builder の build 時に `verify_lineage_closure` / `verify_edge_namespaces` で機械的に弾かれる (二重防御)。decompose 段階で潰しておくほうがやり直しが安い。各項目の詳細は [references/decompose-self-check.md](references/decompose-self-check.md)。

## 出力契約

本 Skill は `context: fork` で動くため、生成物は **必ず `output_dir` 配下にファイル** として出力する。フローの長短にかかわらず inline 返しはしない。

| mode | 出力ファイル |
|---|---|
| `analyze` | `<output_dir>/analysis-<flow>.md` |
| `decompose` | `<output_dir>/decomposition-plan-<flow>.md` |
| `both` | 上記両方 |

メイン会話への戻り値は **実行サマリのみ**: 書いたファイルパス・主要な判断 (推定レイヤ構成・分解候補数)・self-check の異常点。生成本文の全文を戻り値に詰めない (fork した意義 = メイン context の保護が損なわれ、後段 prep-builder はこれらをファイル参照前提で読むため契約破壊になる)。

**出力量は最小化する** — 本 Skill は fork 内で大量の MD を書き出すため write token が wall clock の支配要因。各 .tfl の Description は 1-2 行、Alternatives considered は非自明な分岐がない限り省略、Upstream lineage の Prev chain は区間表記 (`#8..#22`)。詳細は [../../../references/decomposition-plan-format.md §Verbosity policy](../../../references/decomposition-plan-format.md)。

戻り値末尾に **`## Timing` ブロック** を必ず含める (フォーマットと推奨 breakdown: [references/skill-timing-contract.md](../../../references/skill-timing-contract.md))。本 Skill の breakdown 項目は `input read` / `analyze` / `decompose` / `self-check` / `write` の 5 項目を最低限カバーする。

Write が失敗した場合は inline fallback せず、その時点で停止して caller にエラーを返す (どのファイルへの書き込みが失敗したか・原因を含む)。

## prep-builder への引き渡し

decompose 完了後、prep-builder に渡すのは:

1. `decomposition-plan-<flow-name>.md`（設計の真の source）
2. 元 .tfl / .tflx（build 時の元ノード定義の参照元）

prep-builder はこの 2 つから新 .tfl 群を生成し、続いて prep-deployer が publish / run を行う。

## 設計原則

- 元 .tfl は本 Skill では絶対に変更しない（build は prep-builder の責務）
- 設計フェーズと実装フェーズを分ける（decompose 完了で必ず一度止まる）
- 未知のノード種別・循環依存等は中断してユーザーに報告
- git をソース・オブ・トゥルースに
