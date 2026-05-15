---
purpose: 新規 .tfl ファイル・ノード・列・Published DS の命名規約
fetched_at: 2026-05-17
note: レイヤ別ファイル名規約 (stg_<source>__<entity> / int_<entity>_<verb> / fct_/dim_/rpt_)、ノード名・列名・Published DS 名のルールを規定
---

# naming-conventions

新規 .tfl ファイル・ノード・列・Published DS の命名規約。prep-architect の decompose（命名決定）、prep-builder（ファイル名生成）、prep-deployer の publish（DS 名決定）で参照される。

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

step 番号は依存順。前段の出力（Hyper）が次段の Input。10+ になる場合は `step01`〜 のゼロパディング。

連鎖分割を採用する判断基準（および原則 1 .tfl にまとめる理由）は [.claude/skills/prep-architect/references/intermediate-decomposition.md](../.claude/skills/prep-architect/references/intermediate-decomposition.md) を参照。

### marts

mart 層は **3 種類のファイル** で構成する:

```
fct_<entity>.tfl       # ファクト。1 ファクト 1 ファイル。再利用素材
dim_<entity>.tfl       # ディメンション。1 dim 1 ファイル。複数 fct で共有
rpt_<scope>.tfl        # fct × dim を Prep 内で JOIN 済みの OBT (one big table)
```

`<entity>` は dbt 慣例に従い **複数形** 推奨（`fct_orders`, `dim_customers`）。`<scope>` は分析タスク名（例: `rpt_sales_with_customer`, `rpt_returns_by_region`）。

**なぜ rpt_ が必要か**: Tableau Workbook の Data Model では **Published Data Source 同士の Relationship / Join は不可** で、結合できるのは Data Blending のみ（非加法集計に制約あり）。複数 dim を組合せた本格分析が必要な場合は、Prep 内で物理 JOIN した結合済み Published DS を rpt_*.tfl として用意するのが現実解。

軽い数値の重ね合わせで足りるケースは fct_ / dim_ 直読 + Data Blending で済ませる。事前集計の OBT は粒度を明示して `agg_<entity>_<grain>`（例: `agg_revenue_monthly`）でも可。

### 区切り文字サマリ

| 区切り | 用途 |
|---|---|
| `_` | 単語区切り（snake_case） |
| `__` | staging の `<source>` と `<entity>` の境界のみ |

## Published Data Source 名

Prep フローが publish する Hyper / DS の名前は **ファイル名と一致** させる。

| .tfl ファイル | Published DS 名 |
|---|---|
| `stg_salesforce__opportunities.tfl` | `stg_salesforce__opportunities` |
| `fct_sales.tfl` | `fct_sales` |
| `dim_customers.tfl` | `dim_customers` |

`_published` サフィックスを付ける／付けないは組織選択。付けると「これは Prep が publish したもの」と明示できるが、冗長。MVP は付けない方針を推奨。

## ノード名（.tfl 内部の Tableau Prep UI 表示名）

- snake_case を維持
- 何をするノードか分かる命名（`Cleaned`, `Joined Customers`, `Pivoted Status` 等）
- 自動生成のデフォルト名（`Step 1`, `Aggregate 1`）は避ける

## 列名

中間列名:
- snake_case
- ビジネス命名（`Order ID` よりも `order_id`）
- 元データの大文字 / 空白入り列名は staging で snake_case に整形

最終出力（marts）の列名は BI 利用者向けに **Title Case** を許容（Tableau 慣例）:

```
staging: order_id, customer_id, total_amount
marts:   Order ID, Customer ID, Total Amount
```

## 中間 Hyper ファイル名

intermediate 連鎖で使う中間 Hyper は生成元 .tfl と同名:

```
int_orders_step1_filter.tfl
  → 出力: int_orders_step1_filter.hyper
  → 次段の Input: int_orders_step1_filter.hyper
```

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
