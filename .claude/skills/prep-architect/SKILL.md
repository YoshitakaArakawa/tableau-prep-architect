---
name: prep-architect
description: prep-extractor が生成した flow-summary.md を入力に、Tableau Prep の長大フローを dbt 流のレイヤ規律で分析・分解設計する。analyze（現状把握）と decompose（分解設計）の 2 フェーズを、ユーザー指示に応じて順次または個別に実行する。既存の .tfl/.tflx を「分析したい」「分解したい」「dbt 風に再構築したい」「最適化したい」と言われたときに起動。実装（.tfl 生成）は prep-builder、publish 以降は prep-deployer が担当。
context: fork
agent: general-purpose
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
| `deploy_context_path` | decompose で推奨 | prep-extractor Phase B の `deploy-context.md`。既存 flow 名衝突回避に使う |
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

analyze の結果を基に、新 .tfl の構成・命名・配置・migration order を設計した `decomposition-plan-<flow-name>.md` を出す。

| 項目 | 内容 |
|---|---|
| 入力 | `flow-summary.md`, `analysis-<flow-name>.md` |
| 出力 | `decomposition-plan-<flow-name>.md`（書式: [../../../references/decomposition-plan-format.md](../../../references/decomposition-plan-format.md)）|
| 主な判断 | レイヤ分割、actions レベル分割、サブプロジェクト配置、移行順序 |
| 主な参照 | [../../../references/layer-responsibilities.md](../../../references/layer-responsibilities.md), [references/intermediate-decomposition.md](references/intermediate-decomposition.md), [../../../references/naming-conventions.md](../../../references/naming-conventions.md), [../../../references/input-policy.md](../../../references/input-policy.md), [../../../references/project-hierarchy.md](../../../references/project-hierarchy.md) |

## How to invoke

ユーザー指示の解釈:

| 指示 | 動作 |
|---|---|
| 「分析して」「現状把握したい」 | analyze のみ実行 |
| 「分解設計して」「.tfl 構成を考えて」 | analyze 未実施なら analyze → decompose、実施済みなら decompose のみ |
| 「再構築したい」「dbt 風に整理して」 | analyze → decompose を順次実行、完了後 prep-builder の起動を案内 |
| flow-summary.md が無い | prep-extractor の起動を案内、本 Skill は中断 |
| 「特定のステップだけ」「intermediate だけ深掘り」 | 該当部分の analyze / decompose に絞る |

analyze と decompose の間で **一度必ずユーザーに確認を取る**。decompose は分解の方針判断を含むため、設計案を生成してから build（prep-builder）に進む前に再確認する。

### decompose 完了前の self-check (必須)

`decomposition-plan-<flow>.md` をユーザーに渡す前に、以下を順に確認する:

1. **Upstream lineage 表が各 .tfl ごとに埋まっているか** ([../../../references/decomposition-plan-format.md](../../../references/decomposition-plan-format.md) の Lineage closure invariant 節)
2. **各 Included step は flow-summary.md の Topology 表で Prev 連鎖を辿ったとき、その .tfl の宣言 Inputs に到達するか** (下流の結合キーから逆推定しない)
3. **削除提案ノード一覧に SuperUnion が含まれていないか** — Union は actions=0 / 入力ブランチが同一に見えても **削除候補にしてはならない**。理由: Union ノードは入力起源を識別する `Table Names` 列を暗黙注入し、下流 RemoveColumns(Table Names) や Join clause が依存しているケースがある。Union 周辺の no-op を畳む提案を出す前に、Union 出力スキーマの参照を下流で全洗いしてからにする。一般化すると「下流のスキーマ依存を Source DAG 全体で完全に再現できると証明できない限り、Union は保持」
4. **`## Output mapping (original → decomposed)` セクションが埋まっているか** — 元フローの全 output PDS と、それを引き継ぐ marts レイヤ flow の対応が表で書かれているか。本表が欠けると prep-builder の `publish_manifest.py init` が失敗し、最終的に prep-output-comparator がペアを組めない ([../../../references/decomposition-plan-format.md](../../../references/decomposition-plan-format.md) の Output mapping 節 / [../../../references/publish-manifest-format.md](../../../references/publish-manifest-format.md))
5. **型変換 / 名前変換が staging レイヤに集中しているか** — intermediate / marts レイヤの `Included original steps` に `ChangeType` (型キャスト) や `Rename` (列名変更) を含む actions が残っていたら、staging の責務漏れの疑い。`Actions-level splits` セクションで該当 actions を stg 側に巻き戻すことを検討する。例外: intermediate での Join 後にしか型が確定しない列 (`derived = TOFLOAT(col_a + col_b)` 等) は intermediate に残して良い
6. **同じソーステーブルに対する Input ノードが staging で 1 回に集約されているか** — 複数の stg .tfl が同一の `Source` (例: `vc_salesforce / Opportunity`) を Input にしていたら統合候補。判定方法: `## New .tfl files` の各 stg セクションの `Inputs` を集計し、同じ Source 名が複数 .tfl に出現していないか確認。例外: 同一テーブルから **異なる列セット** を取り出して別ドメインに供給するケース (`stg_orders__metrics` と `stg_orders__metadata` が同じ `Orders` テーブルから別目的で columns を抜く) は分離保持で良い
7. **`## Dependency DAG (Mermaid)` に Before / After 2 ブロックが揃っているか** ([../../../references/decomposition-plan-format.md](../../../references/decomposition-plan-format.md) の Dependency DAG 節に意義と書式ルール)
8. **Join を含む .tfl に `**Joins**` フィールドが書かれ、cardinality (1:1 / 1:N / N:N / 不明) が記載されているか** — SuperJoin ノード、または .tfl 内で Join を行うステップを含む .tfl で必須。不明な場合も `不明` と明示する (空欄不可、書式詳細は [../../../references/decomposition-plan-format.md](../../../references/decomposition-plan-format.md) の Joins field の書式 節)
9. **marts レイヤに残っている AddCol actions を上流 (intermediate) で実施できないか検討したか** — marts レイヤの `Included original steps` に `AddCol` (計算フィールド追加) actions が含まれていたら、その計算を intermediate 段で実施できないかを検討する。検討の結果 marts 残置が妥当な場合はそれで良い (例: BI 表示用整形、行単位の派生列でかつ他フローから再利用しないもの)。判定は user 側に判断材料を提示する形で良い
10. **marts レイヤに残っている Filter actions を上流 (intermediate / staging) で実施できないか検討したか** — marts レイヤの `Included original steps` に Filter actions が含まれていたら、その Filter を上流段で実施して行数削減を前倒しできないかを検討する。検討の結果 marts 残置が妥当な場合はそれで良い (例: marts 固有のサンプリング・上位 N 件、後段で full history が必要)。判定は user 側に判断材料を提示する形で良い
11. 1〜10 で不整合や検討漏れが見つかったら、是正してから plan を出力する

省略すると prep-builder の build 時に `verify_lineage_closure` / `verify_edge_namespaces` で機械的に弾かれる (二重防御)。decompose 段階で潰しておくほうがやり直しが安い。

## 出力契約

本 Skill は `context: fork` で動くため、生成物は **必ず `output_dir` 配下にファイル** として出力する。フローの長短にかかわらず inline 返しはしない。

| mode | 出力ファイル |
|---|---|
| `analyze` | `<output_dir>/analysis-<flow>.md` |
| `decompose` | `<output_dir>/decomposition-plan-<flow>.md` |
| `both` | 上記両方 |

メイン会話への戻り値は **実行サマリのみ**: 書いたファイルパス・主要な判断 (推定レイヤ構成・分解候補数)・self-check の異常点。生成本文の全文を戻り値に詰めない (fork した意義 = メイン context の保護が損なわれ、後段 prep-builder はこれらをファイル参照前提で読むため契約破壊になる)。

Write が失敗した場合は inline fallback せず、その時点で停止して caller にエラーを返す (どのファイルへの書き込みが失敗したか・原因を含む)。

## prep-builder への引き渡し

decompose 完了後、prep-builder に渡すのは:

1. `decomposition-plan-<flow-name>.md`（設計の真の source）
2. 元 .tfl / .tflx（build 時の元ノード定義の参照元）

prep-builder はこの 2 つから新 .tfl 群を生成し、続いて prep-deployer が publish / run / test を行う。

## 設計原則

- 元 .tfl は本 Skill では絶対に変更しない（build は prep-builder の責務）
- 設計フェーズと実装フェーズを分ける（decompose 完了で必ず一度止まる）
- 未知のノード種別・循環依存等は中断してユーザーに報告
- git をソース・オブ・トゥルースに
