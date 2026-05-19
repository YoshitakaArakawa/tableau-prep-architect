---
purpose: prep-output-comparator が出力する comparison-report.md と comparison-report.json のフォーマット仕様
fetched_at: 2026-05-19
note: JSON は v1 で固定された API contract。Markdown は人間用、JSON は機械用で両者は同じ事実を別表現。後段 (prep-builder / prep-deployer の再呼び出し判断) は JSON を消費することを前提に設計
---

# Report Format

`prep-output-comparator` の出力 2 ファイル `comparison-report.md` と `comparison-report.json` のフォーマット仕様。

## 目次

- ファイル配置
- JSON スキーマ
- Markdown 構造
- 値の精度と表示

## ファイル配置

caller から渡された `output_dir` (典型: `work/<yyyymmdd>_<tag>/reports/`) の直下に 2 ファイルを置く:

```
<output_dir>/
├── comparison-report.md
└── comparison-report.json
```

`pairs.json` (Step 1 のペア解決中間ファイル) も同じ directory に残してよい (caller がデバッグで参照する用途)。`work/` 配下の役割分離は [CLAUDE.md §work/ ディレクトリ規約](../../../../CLAUDE.md#work-ディレクトリ規約) 参照。

## JSON スキーマ

### トップレベル

```json
{
  "schema_version": "1",
  "generated_at": "2026-05-19T10:00:00+09:00",
  "tolerance": { "ratio": 0.01 },
  "original_flow_luid": "<luid>",
  "new_flow_luids": ["<luid>", "..."],
  "pairs": [ /* Pair object × N */ ],
  "overall_verdict": "pass" | "fail",
  "summary": {
    "pair_count": 2,
    "pass_count": 0,
    "fail_count": 2,
    "flags_observed": ["clean_2x_multiple", "table_names_residual", "..."]
  }
}
```

- `schema_version`: 文字列。v1 では `"1"` 固定。スキーマ変更時にインクリメント
- `generated_at`: ISO-8601 with timezone offset。JST (+09:00) を推奨
- `tolerance.ratio`: 浮動小数比較の許容誤差 (デフォルト 0.01 = ±1%)。caller が上書き可能

### Pair object

```json
{
  "pair_index": 0,
  "original": {
    "luid": "<luid>",
    "name": "stockmarket_transaction_prepped",
    "project_name": "0_Datasource",
    "project_luid": "<project-luid>"
  },
  "new": {
    "luid": "<luid>",
    "name": "fct_transactions_summary",
    "project_name": "marts",
    "project_luid": "<project-luid>"
  },
  "schema_diff":  { /* see below */ },
  "size_diff":    { /* see below */ },
  "value_diff":   { /* see below */ },
  "flags":        ["clean_2x_multiple", "table_names_residual"],
  "verdict":      "pass" | "fail"
}
```

### `schema_diff`

```json
{
  "original_only": [
    { "name": "Date", "dataType": "DATE", "role": "DIMENSION" }
  ],
  "new_only": [
    { "name": "Table Names-1", "dataType": "STRING", "role": "DIMENSION" }
  ],
  "dataType_mismatch": [
    { "name": "row_num", "original_dataType": "INTEGER", "new_dataType": "REAL" }
  ],
  "common": [
    { "name": "銘柄", "dataType": "STRING", "role": "DIMENSION" }
  ]
}
```

- 各 entry の最低必須キー: `name`, `dataType`, `role`
- `common` は両 DS に同じ name / dataType / role で存在する列のフラットなリスト
- `original_only` / `new_only` の判定はキー `name` の完全一致のみ (大文字小文字・空白を含む)。type だけ違うものは `dataType_mismatch` 行きで、`original_only` / `new_only` には入れない

### `size_diff`

```json
{
  "total": {
    "original_rows": 45,
    "new_rows": 102,
    "ratio_new_over_original": 2.2667,
    "match_within_tolerance": false
  },
  "by_key": {
    "key_columns": ["銘柄"],
    "rows": [
      {
        "key_values": { "銘柄": "TSM" },
        "original_rows": 7,
        "new_rows": 14,
        "ratio_new_over_original": 2.0,
        "match_within_tolerance": false
      }
    ]
  }
}
```

- `by_key` は `key_columns` が caller から渡された場合のみ含める。なければ `null`
- `ratio_new_over_original`: `new_rows / original_rows`。`original_rows == 0` のとき null
- `match_within_tolerance`: `|ratio - 1| <= tolerance.ratio` のとき true

### `value_diff`

```json
{
  "measure_columns": ["数量", "収支"],
  "split_dimension": "取引",
  "rows": [
    {
      "split_value": "買付",
      "measure": "数量",
      "original_sum": 396,
      "new_sum": 792,
      "ratio_new_over_original": 2.0,
      "match_within_tolerance": false
    }
  ]
}
```

- `measure_columns`: caller 指定または自動選択された列リスト
- `split_dimension`: 値比較時の分割 dimension (本 Skill では `取引` のような業務分類列を 1 つ選ぶ。`key_columns[0]` を流用するか、別途指定)
- `rows`: split × measure の cross-tab。`split_dimension` の値ごと、`measure_columns` の各列ごとに 1 行

### `flags`

文字列の配列。値は `SKILL.md §Step 5 パターンフラグ検出` の表に列挙されたものから 0 個以上。

### `verdict`

`pass` | `fail`。判定基準は SKILL.md §判定基準 参照。

## Markdown 構造

JSON と同じ情報を読みやすく整形する。1 ペア = 1 セクション。テンプレート:

```markdown
# Comparison Report

- Generated at: 2026-05-19T10:00:00+09:00
- Tolerance: ±1%
- Original flow LUID: ...
- New flow LUIDs: ..., ...
- **Overall verdict: FAIL** (0 pass / 2 fail / 2 pairs)

## Pair 0: stockmarket_transaction_prepped → fct_transactions_summary

- Original: `0_Datasource / stockmarket_transaction_prepped` (LUID `...`)
- New: `marts / fct_transactions_summary` (LUID `...`)
- **Verdict: FAIL**
- Flags: `clean_2x_multiple`, `table_names_residual`

### Schema diff

新側だけにある列 (1):

| 列名 | dataType | role |
|---|---|---|
| Table Names-1 | STRING | DIMENSION |

元側だけにある列: なし
dataType 不一致: なし
共通列数: 19

### Size diff

| 観点 | 元 | 新 | 倍率 | 許容内? |
|---|---|---|---|---|
| 全体 | 45 | 102 | 2.27 | ❌ |

キー別 (`銘柄`):

| 銘柄 | 元 | 新 | 倍率 | 許容内? |
|---|---|---|---|---|
| TSM | 7 | 14 | 2.00 | ❌ |
| ... | ... | ... | ... | ... |

### Value diff

`取引` で分割、measure `数量`, `収支`:

| 取引 | measure | 元 SUM | 新 SUM | 倍率 | 許容内? |
|---|---|---|---|---|---|
| 買付 | 数量 | 396 | 792 | 2.00 | ❌ |
| 買付 | 収支 | -6,928,156.67 | -13,856,313.33 | 2.00 | ❌ |
| ... | ... | ... | ... | ... | ... |

---

## Pair 1: ...
```

各ペアの末尾には `---` (HR) を入れる。

## 値の精度と表示

- **JSON の数値**: そのまま JSON Number として書く (浮動小数は IEEE 754 double 相当)。丸めない
- **Markdown の数値**: 表示用に丸める。整数は整数のまま、浮動小数は 2 桁
- **倍率 (ratio)**: 小数第 4 位まで保持 (JSON)、Markdown では 2 桁表示
- **`match_within_tolerance`**: JSON で boolean、Markdown では `✅` / `❌`

## サンプル

本フォーマット策定前の PoC ドラフトは破棄済み。本仕様準拠の最初のレポートが本 Skill の初稿出力となる。
