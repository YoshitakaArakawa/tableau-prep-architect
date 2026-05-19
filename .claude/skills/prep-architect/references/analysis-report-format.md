---
purpose: prep-architect の analyze フェーズが出力する分析レポートの markdown 書式仕様
fetched_at: 2026-05-17
note: レポートのトップレベル構造、必須セクション、各セクションの記述ルールを規定
---

# analysis-report-format

**analyze フェーズ**の出力——分析レポートの書式と必須セクションを定義する。Skill が出力する markdown の構造を統一し、後続の **decompose フェーズ**が機械的に読めるようにする。

## レポートのトップレベル構造

必須セクション（順序固定）：

```markdown
# Analysis: <flow-name>

## Meta
## Steps
## Inputs / Outputs
## Input Compliance
## Decomposition points
## Notes / Warnings
```

`Input Compliance` セクションは [../../../../references/input-policy.md](../../../../references/input-policy.md) の判定基準に従う。

## 各セクションの書式

### Meta

```markdown
## Meta
- Source path: ./flows/legacy.tflx
- Tableau version: 2026.1
- Total steps: 47
- Analyzed at: 2026-05-15
```

### Steps

ノード一覧を表形式で。**SuperTransform の場合は `Actions summary` 列で中身を 1 行に要約**：

```markdown
## Steps
| # | Type | Name | Depends on | Actions summary | Layer (推定) | Notes |
|---|---|---|---|---|---|---|
| 1 | LoadSqlProxy | orders_raw | — | — | stg | Published DS 経由 |
| 2 | SuperTransform | Clean 1 | 1 | Rename×4 + AddCol×1(ROW_NUMBER LOD) | stg/int 境界 | 列名統一＋行番号採番（actions 単位で分割候補） |
| 3 | SuperTransform | Clean 2 | 2 | FIXED MAX + Filter + Rename×2 | int | 最新値抽出 |
| 4 | SuperJoin | Join 1 | 2,3 | — | int 境界 | 複数ソース結合 |
| 5 | SuperAggregate | per_customer | 4 | — | int | 行→顧客集約（粒度変化） |
| 6 | PublishExtract | Output | 5 | — | mart | Published DS 出力 |
| ... |
```

カラムの意味：
- `#`: ステップ番号（元 .tfl 内のノード ID の **topological short ID** と対応）
- `Type`: nodeType（最後のドット以降。例: `SuperTransform`、`SuperJoin`、`PublishExtract`）
- `Name`: ノードの表示名（Tableau Prep UI 上の名前）
- `Depends on`: 依存元のステップ番号（カンマ区切り）
- **`Actions summary`**: SuperTransform の中身要約。`<type>×<count>(<内容ヒント>)` をカンマ区切り。LOD 式や業務ロジックは括弧で簡潔に明示。SuperTransform 以外は `—`
- `Layer (推定)`: stg / int / mart（[../../../../references/layer-responsibilities.md](../../../../references/layer-responsibilities.md) ＋ [../../../../references/prep-ui-to-json-mapping.md](../../../../references/prep-ui-to-json-mapping.md) の actions 表）
- `Notes`: 補足（業務ドメイン名、特殊操作、警告など）

### Inputs / Outputs

```markdown
## Inputs / Outputs

### Inputs
| # | Source | Connection type | Compliance |
|---|---|---|---|
| 1 | Snowflake.PUBLIC.ORDERS | Native DB | ❌ Direct DB connection（input-policy 違反） |
| 8 | tableau-cloud://...customers_vc | Virtual Connection | ✅ Compliant |

### Outputs
| # | Target | Type |
|---|---|---|
| 42 | ./outputs/sales.hyper | Hyper |
| 47 | Published DS: fct_sales | Published Data Source |
```

### Input Compliance

判定基準は [../../../../references/input-policy.md](../../../../references/input-policy.md):「Input ノードは Published Data Source または仮想接続を指すこと」。

```markdown
## Input Compliance

| Input # | Source | Compliance | Migration suggestion |
|---|---|---|---|
| 1 | Snowflake.PUBLIC.ORDERS | ❌ Direct | Create virtual connection `vc_snowflake_orders` |
| 8 | customers_vc | ✅ Compliant | — |

**違反件数: 1 / 全 Input 2 件**
```

### Decomposition points

```markdown
## Decomposition points

| Suggested boundary | Between steps | Rationale |
|---|---|---|
| stg / int の境界 | Step 4 直前 | ここで複数ソースが合流（JOIN 発生） |
| int 内の細分化 | Step 18 / 19 の間 | 粒度変化（行レベル → 顧客レベル集約） |
| int / mart の境界 | Step 38 直前 | 出力ノード直前の最終集約に入る |

**推奨分解結果（プレビュー）:**
- stg: 2 ファイル（ノード 1-3、ノード 8）
- int: 3 ファイル（ノード 4-18、19-30、31-37）
- mart: 2〜3 ファイル（fct + dim を分離。Workbook で複数 dim を組合せる場合は rpt を追加）
```

### Notes / Warnings

```markdown
## Notes / Warnings

- ⚠️ Step 22 は未知の nodeType `CustomTransform`。レイヤ推定保留
- ⚠️ Step 35 で循環依存の疑い（Step 35 → 38 → 35 のように見える、要確認）
- 💡 Step 40 の Python ステップは Prep 必須。intermediate 末尾に配置するのが妥当
- 🔒 Step 10 SuperUnion (Union 3): Union ノードは `Table Names` 列を暗黙注入する → 削除候補にしない
```

> **必須ルール**: flow-summary.md の Topology 表に SuperUnion が登場したら、Notes / Warnings に上記形式の `🔒` 行を **機械的に必ず 1 行追加** すること (該当 Union ごとに 1 行)。これは decompose 側 self-check (Union を削除候補にしない) の入口チェック。analyze の見落としを構造的に塞ぐ二重防御。

## 巨大フローの場合

100+ ステップの大規模フローでは：

- **サマリだけ会話に出力** し、フルレポートは `analysis-<flow-name>.md` ファイルに書き出す
- Steps テーブルは全件記載（省略しない）— decompose が読むため
- Notes / Warnings に「ステップ数が多いため詳細はファイル参照」と明記

## 入力（前提）

analyze は **`flow-summary.md` のみを入力** とする。flow.json は読まない。

`flow-summary.md` は別 Skill `prep-extractor` が生成する（[../../prep-extractor/SKILL.md](../../prep-extractor/SKILL.md)）。analyze 開始時に `flow-summary.md` が無い場合は、まず `prep-extractor` を起動するよう案内する。

`flow-summary.md` から analyze が読み取るセクション:

| flow-summary.md セクション | analyze での使い方 |
|---|---|
| Meta | `analysis.md` の Meta セクションへ転記 |
| Topology | Steps テーブルの骨格（#, Type, Name, Depends on）として転用 |
| Dependency DAG | 依存関係の確認用、レイヤ境界の検討 |
| SuperTransform actions inventory | Steps テーブルの `Actions summary` 列、actions 単位のレイヤ判定材料 |
| Warnings | `Notes / Warnings` セクションへ転記＋業務観点で補強 |

analyze が flow-summary.md に加える価値は **「業務的解釈」**: Layer 推定、Input Compliance、Decomposition points、business overview 等。**構造抽出は extract に任せ、analyze は解釈に集中する**。

## 出力先

`<output_dir>/analysis-<flow-name>.md` に必ずファイル出力する。会話への戻り値は実行サマリのみ ([SKILL.md §出力契約](../SKILL.md#出力契約))。

## decompose への引き継ぎ

decompose フェーズは、本レポートの **以下のセクション** を入力として利用：

- `Steps` テーブル（特に Layer 推定列）
- `Input Compliance`（仮想接続化提案へ）
- `Decomposition points`（分解設計の出発点）

書式が乱れると decompose が誤読するので、**必ず本テンプレに従う**。
