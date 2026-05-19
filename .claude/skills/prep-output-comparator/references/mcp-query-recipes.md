---
purpose: Tableau MCP (list-datasources / get-datasource-metadata / query-datasource) を prep-output-comparator から叩く際の癖と回避策
fetched_at: 2026-05-19
note: PoC で実機確認した挙動を集約。MCP の API ドキュメント代わりではなく、本 Skill が引っかかった落とし穴のみを記載
---

# MCP Query Recipes

Tableau MCP を本 Skill から叩く際の運用上の癖と回避策。

## 目次

- 並列叩きで 401 が出る
- list-datasources の `in` フィルタが効かない
- query-datasource はフィールド caption (内部 ID ではない)
- query-datasource のフィールド構造
- Unicode フィールド名の扱い

## 並列叩きで 401 が出る

Tableau MCP の `get-datasource-metadata` を **4 並列で叩くと、最初の 1 件は通るが残り 3 件が HTTP 401** で返ってくる症状がある。MCP サーバー側の認証セッション初期化レースだと思われる (最初の呼び出しで session を確立、確立完了前の後続が認証なしで叩いてしまう)。

**回避**: ペアごとに **sequential** で叩く。N ペアあるなら 2N 回の sequential 呼び出し (元 / 新 それぞれ 1 回ずつ)。

`list-datasources` と `query-datasource` も予防的に sequential を推奨。

## list-datasources の `in` フィルタが効かない

ドキュメント上は `name:in:[name1,name2,...]` のような `in` 演算子が宣言されているが、実際に叩くと:

```
Error: Invalid filter expression format: "name2"
```

のように途中の値で parse エラーになる。複数 DS を引きたい場合は **`name:eq:<name>` を DS 数ぶん sequential で叩く** のが現実的。

```text
# OK
list-datasources filter=name:eq:fct_transactions_summary
list-datasources filter=name:eq:fct_transactions_matched

# NG
list-datasources filter=name:in:[fct_transactions_summary,fct_transactions_matched]
list-datasources filter=name:in:fct_transactions_summary,fct_transactions_matched
```

なお、本 Skill では PDS の LUID 解決は Metadata API で行うため `list-datasources` を叩く機会は基本ない (caller が DS 名で起動した場合のみ補助的に使用)。

## query-datasource はフィールド caption (内部 ID ではない)

`query-datasource` の `fieldCaption` パラメータは **DS 上の表示名** (`銘柄`, `Current Price` 等) を渡す。内部 ID (`[Calculation_xxxx]` のような) ではない。

`get-datasource-metadata` のレスポンスの `fields[].name` がそのまま `fieldCaption` として使える:

```json
{
  "fields": [
    { "name": "銘柄", "dataType": "STRING", "role": "DIMENSION" },
    ...
  ]
}
```

→ `query-datasource` で `{"fieldCaption": "銘柄"}` のように渡す。

## query-datasource のフィールド構造

集計あり / なし / 計算 / bin の 4 形態がある。本 Skill で使うのは前 2 つ:

### Dimension (集計なし)

```json
{ "fieldCaption": "銘柄" }
```

### Measure (集計あり)

```json
{ "fieldCaption": "数量", "function": "SUM", "fieldAlias": "qty_sum" }
```

`function` に取れる値: `SUM`, `AVG`, `MEDIAN`, `COUNT`, `COUNTD`, `MIN`, `MAX`, `STDEV`, `VAR`, `COLLECT`, `YEAR`, `QUARTER`, `MONTH`, `WEEK`, `DAY`, `TRUNC_YEAR` 系, `AGG`, `NONE`, `UNSPECIFIED`。

### 全体行数を取るレシピ

「全行カウント」は専用の関数がない。任意の dimension 列を 1 つ選んで `COUNT` を掛ける:

```json
{
  "fields": [
    { "fieldCaption": "銘柄", "function": "COUNT", "fieldAlias": "row_count" }
  ]
}
```

`銘柄` が NULL を含まないキーであれば全行数になる。NULL を含む列を使うと NULL 行が落ちて実際より少ない値が返るので、**NULL を含まないキー列を選ぶ** こと (本 Skill の `key_columns` 引数で指定された列を優先)。

### キー別行数 + ソート

```json
{
  "fields": [
    { "fieldCaption": "銘柄" },
    { "fieldCaption": "銘柄", "function": "COUNT", "fieldAlias": "rows",
      "sortDirection": "DESC", "sortPriority": 1 }
  ]
}
```

同じ列を dimension としても measure としても並べる (group by + count of group)。`sortPriority` は 1 から始まる整数。

### 複数 dimension での分割

```json
{
  "fields": [
    { "fieldCaption": "取引" },
    { "fieldCaption": "Table Names-1" },
    { "fieldCaption": "銘柄", "function": "COUNT", "fieldAlias": "rows" }
  ]
}
```

dimension を 2 つ並べれば cross-tab になる。本 Skill では `Table Names-*` の残存検出時に活用 (フラグ `table_names_residual` 立てた後で詳細を見たいとき)。

## Unicode フィールド名の扱い

日本語列名はそのまま渡せるが、JSON ペイロードで `\uXXXX` エスケープが必要な MCP クライアント実装もある。SDK 側で対応するなら:

```python
import json
payload = json.dumps({"fields": [{"fieldCaption": "銘柄", "function": "COUNT"}]},
                     ensure_ascii=True)  # → "銘柄" に変換される
```

Tableau MCP は両方受け付ける。本 Skill から叩くときはどちらでもよい。

## レスポンスの読み方

`query-datasource` のレスポンスは `{ "data": [{...row}, {...row}, ...] }` の形。`row` のキーは `fieldAlias` (指定した場合) または `fieldCaption` (デフォルト)。

```json
{ "data": [
  { "取引": "買付", "rows": 25 },
  { "取引": "売付", "rows": 13 }
]}
```

dimension が複数あれば、`row` にすべての dimension 値と measure 値が並ぶ。

NULL は JSON の `null` で返る (Tableau の「null」と区別したい場合、本 Skill では現状特別扱いしないが、将来 `qty_sum: null` のような行を「該当行なし」と判定したい場合は分岐を入れる)。
