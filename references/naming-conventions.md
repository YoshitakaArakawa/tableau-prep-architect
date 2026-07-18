---
purpose: 新規 .tfl ファイル・ノード・列・Published DS の命名規約
note: レイヤ別ファイル名規約 (stg_<source>__<entity> / int_<entity>_<verb> / fct_/dim_/rpt_)、ノード名・列名・Published DS 名のルールを規定
---

# naming-conventions

新規 .tfl ファイル・ノード・列・Published DS の命名規約。tableau-prep-architect の decompose（命名決定）、tableau-prep-builder（ファイル名生成）、tableau-prep-deployer の publish（DS 名決定）で参照される。

## ファイル名規約

レイヤごとに固定プレフィクス。ファイル名から一発でレイヤと役割を判別できる。

### staging

```
stg_<source>__<entity>.tfl
```

- `<source>`: データソース識別子。snake_case（例: `salesforce`, `snowflake`, `s3_logs`）
- `<entity>`: 対象テーブル / オブジェクト名。snake_case（例: `opportunities`, `orders`, `clickstream`）
- 区切り: `<source>` と `<entity>` の間は **ダブルアンダースコア `__`**（dbt 規約、視認性向上）

例:
- `stg_salesforce__opportunities.tfl`
- `stg_snowflake__orders.tfl`

### intermediate

```
int_<entity>_<verb>.tfl
```

- `<entity>`: 主たる対象（例: `orders`, `customer`, `sales`）
- `<verb>`: 何をするか（例: `joined`, `pivoted`, `categorized`, `aggregated`）
- 区切り: 全てシングルアンダースコア `_`

例:
- `int_orders_joined.tfl`
- `int_customers_categorized.tfl`

### intermediate の連鎖分割（例外時のみ）

intermediate は **原則 1 entity 1 .tfl**。やむを得ず連鎖分割する場合の命名:

```
int_<entity>_step<N>_<verb>.tfl
```

例:
- `int_orders_step1_filter.tfl`
- `int_orders_step2_join_customers.tfl`
- `int_orders_step3_categorize.tfl`

step 番号は依存順。前段の出力（Published DS）が次段の Input（cross-flow chain は全層 PDS 経由、Hyper file 出力は cross-flow 共有不可。[input-policy.md](input-policy.md)）。10+ になる場合は `step01`〜 のゼロパディング。

連鎖分割を採用する判断基準（および原則 1 .tfl にまとめる理由）は [.claude/skills/tableau-prep-architect/references/intermediate-decomposition.md](../.claude/skills/tableau-prep-architect/references/intermediate-decomposition.md) を参照。

### marts

mart 層は **3 本立て（fct_ / dim_ / rpt_）+ 派生の agg_** で構成する:

```
fct_<entity>.tfl       # ファクト。1 ファクト 1 ファイル。再利用素材
dim_<entity>.tfl       # ディメンション。1 dim 1 ファイル。複数 fct で共有
rpt_<scope>.tfl        # fct × dim を Prep 内で JOIN 済みの OBT (one big table)
agg_<entity>_<grain>.tfl  # fct から派生する事前集計 OBT。粒度を名前に明示
```

`<entity>` は dbt 慣例に従い **複数形** 推奨（`fct_orders`, `dim_customers`）。`<scope>` は分析タスク名（例: `rpt_sales_with_customer`, `rpt_returns_by_region`）。

rpt_ / agg_ を作る判断基準と理由 (Workbook の Data Blending 制約) は [layer-responsibilities.md](layer-responsibilities.md#fct--dim--rpt--agg-の役割分担) を参照。事前集計の OBT は粒度を明示して `agg_<entity>_<grain>`（例: `agg_revenue_monthly`）と命名する。

### 区切り文字サマリ

| 区切り | 用途 |
|---|---|
| `_` | 単語区切り（snake_case） |
| `__` | staging の `<source>` と `<entity>` の境界のみ |

## Published Data Source 名

Prep フローが publish する Published DS の名前は **.tfl ファイル名と一致** (拡張子なし) を default 規約とする。

| .tfl ファイル | Published DS 名 |
|---|---|
| `stg_salesforce__opportunities.tfl` | `stg_salesforce__opportunities` |
| `fct_sales.tfl` | `fct_sales` |
| `dim_customers.tfl` | `dim_customers` |

- `_published` サフィックスを付ける／付けないは組織選択。付けると「これは Prep が publish したもの」と明示できるが冗長。MVP は付けない方針を推奨
- 例外: 同一 .tfl 内で複数 PublishExtract ノードを持ち別 PDS 名にしたい場合は decomposition-plan の Output mapping で別名を明示 ([decomposition-plan-format.md](decomposition-plan-format.md))

## ノード名（.tfl 内部の Tableau Prep UI 表示名）

- snake_case を維持
- 何をするノードか分かる命名（`Cleaned`, `Joined Customers`, `Pivoted Status` 等）
- 自動生成のデフォルト名（`Step 1`, `Aggregate 1`）は避ける

## 列名

**列名は本ファイルの管轄外**。本ファイルが規定するのは .tfl ファイル名・ノード名・Published DS 名で、**列名の規約は [input-policy.md §命名レジーム](input-policy.md) が正典**。要点:

- 分解後の各層が公開する列名は **元フローの内部名を end-to-end verbatim 保持** する。snake_case や英語への意訳 (semantic translation) は **行わない**
- 理由: tableau-prep-builder は下流ノードを式ごと verbatim 転写し列参照を書き換えないため、上流 PDS が別名を公開すると下流 run が "Unknown field name" で fail する
- 元 output を引き継ぐ mart は元 output PDS とスキーマ完全一致 (列名含む) で publish する。命名レジームの下ではこれは自動達成され、Rename-back は上流で divergent な forward rename を導入した例外時のみ必要 ([decomposition-plan-format.md §Rename-back](decomposition-plan-format.md#rename-back-mart-境界の-presentation-rename))
- BI 向けの表示名変更は mart 境界より下流 (PDS の caption / Workbook 側) で行う

## アンチパターン

| 避ける | 理由 |
|---|---|
| `Orders.tfl`（プレフィクスなし） | レイヤが分からない |
| `stg_orders_v2.tfl`（バージョン番号付き） | git で版管理する。バージョンは履歴で追える |
| `FCT_SALES.tfl`（大文字） | snake_case に統一 |
| `int_orders-joined.tfl`（ハイフン） | `_` に統一 |
| `stg_sf__opp.tfl`（過度な略語） | 可読性低下 |
| `int_orders_step10.tfl`（パディングなし） | 10+ なら `step01`〜 のゼロパディング |

## 例外

- 一時的な PoC / scratch: `_scratch_` プレフィクス（git に入れない）
- 廃止予定: `_deprecated_` プレフィクス（次のリリースで削除）
