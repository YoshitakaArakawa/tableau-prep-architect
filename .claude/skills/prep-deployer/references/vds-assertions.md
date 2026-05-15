---
purpose: VizQL Data Service (VDS) を使った Published DS のデータ品質アサーション設計案
sources:
  - https://help.tableau.com/current/api/vizql-data-service/en-us/
  - https://docs.getdbt.com/docs/build/data-tests
fetched_at: 2026-05-17
note: dbt の not_null / unique / row_count / accepted_values / relationships を VDS クエリで実現する仕様の検討メモ。実装スクリプトは未着手のドラフト段階
---

# vds-assertions

VizQL Data Service (VDS) を使った、Published Data Source へのデータ品質アサーション。dbt の `not_null` / `unique` / `relationships` / `accepted_values` テストに相当する仕組みを Tableau エコシステム内で実現する。

**Test フェーズ用ドラフト**。実装スクリプト (`test_published_ds.py`) は未実装で、本ファイルは仕様検討メモ。

## VDS の基本

Tableau 2024.2+ で GA。Published Data Source に対して **プログラマブルに HTTP クエリ** を投げられる。BI ツール（Tableau Desktop）を経由せずに DS の中身を取得できる。

主要エンドポイント:

```http
POST /api/v1/vizql-data-service/query-datasource
Content-Type: application/json
Authorization: X-Tableau-Auth: <token>

{
  "datasource": { "datasourceLuid": "<luid>" },
  "query": {
    "fields": [
      { "fieldCaption": "Order Date" },
      { "fieldCaption": "Sales", "function": "SUM" }
    ],
    "filters": [...]
  }
}
```

→ JSON 形式で集計結果が返る。

## アサーション種別と VDS マッピング

| アサーション | dbt 例 | VDS クエリ |
|---|---|---|
| **`not_null`** | カラム X に NULL が無い | `COUNT(*) WHERE X IS NULL` → 0 を期待 |
| **`unique`** | カラム X が一意 | `COUNT(*) - COUNT(DISTINCT X)` → 0 を期待 |
| **`row_count`** | 行数が min..max の範囲 | `COUNT(*)` → 範囲内チェック |
| **`accepted_values`** | カラム X の値が指定セットに含まれる | `COUNT(*) WHERE X NOT IN (...)` → 0 を期待 |
| **`relationships`** | fct.dim_id が dim.id に必ず存在 | fct と dim を別 VDS クエリ、差集合を計算 |

## アサーション定義の YAML 書式（案）

dbt の `schema.yml` に倣う：

```yaml
# tests/fct_sales_assertions.yml
datasource: fct_sales_published
assertions:
  - column: order_id
    test: not_null
  - column: order_id
    test: unique
  - test: row_count
    min: 1000
    max: 10_000_000
  - column: status
    test: accepted_values
    values: [pending, completed, cancelled, refunded]
  - column: customer_id
    test: relationships
    to: dim_customer_published
    field: customer_id
```

## 失敗時の挙動（案）

| 状況 | exit code | 出力 |
|---|---|---|
| 全アサーション pass | 0 | `[PASS] 12 assertions on fct_sales_published` |
| 1+ assertion fail | 1 | `[FAIL] fct_sales_published.order_id: 3 nulls found (expected 0)` |
| VDS クエリ自体が失敗 | 2 | `[ERROR] VDS query failed: <error>` |

## CI への組み込み

```yaml
# .github/workflows/test.yml
- run: python test_published_ds.py --config tests/fct_sales_assertions.yml
```

publish + run の後に test ステップを置く形。fail したら deploy パイプラインを止める。

## 実装スコープ

Test フェーズで実装する想定のスクリプト:

- `scripts/test_published_ds.py` — YAML を読み、VDS クエリを順次投げ、結果を集計
- アサーション種別ごとのクエリビルダー
- 失敗時の詳細レポート出力

## 補足：dbt テストとの違い

| dbt | VDS アサーション |
|---|---|
| ウェアハウス上で SELECT 実行 | Tableau Cloud 上で VDS クエリ実行 |
| `dbt test` で全体実行 | スクリプト経由で全体実行（CI 組み込み） |
| `not_null`, `unique`, `relationships`, `accepted_values` 等を built-in | 同等のアサーションを VDS クエリで自前実装 |
| `custom_data_tests` で任意 SQL | 任意 VDS クエリで実現可（カスタムフィルタ・集計） |

## 参考

- [VizQL Data Service docs](https://help.tableau.com/current/api/vizql-data-service/en-us/)
- [dbt tests reference](https://docs.getdbt.com/docs/build/data-tests)（インスピレーション源）
