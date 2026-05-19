---
purpose: prep-output-comparator が出力する comparison-report.md のフォーマット仕様
fetched_at: 2026-05-19
note: 本 Skill は MD 単一出力。後段消費者は人間とメインエージェント LLM。プログラマティック消費 (後段 Skill の自動連携) の必要が生じたら schema 付き JSON を再導入する余地は残す
---

# Report Format

`prep-output-comparator` が出力する `comparison-report.md` の Markdown 構造。

## 目次

- ファイル配置
- 構造
- フラグ一覧
- 判定基準

## ファイル配置

caller から渡された `output_dir` (典型: `work/<yyyymmdd>_<tag>/reports/`) の直下に書く:

```
<output_dir>/
└── comparison-report.md
```

`pairs.json` (Step 1 のペア解決中間ファイル) も同じ directory に残してよい (デバッグ用)。`work/` 配下の役割分離は [CLAUDE.md §work/ ディレクトリ規約](../../../../CLAUDE.md#work-ディレクトリ規約) 参照。

## 構造

### ヘッダ

```markdown
# Comparison Report

- Generated at: 2026-05-19T10:00:00+09:00
- Original flow LUID: <luid>
- New flow LUIDs: <luid>, <luid>
- **Overall verdict: FAIL** (0 pass / 2 fail / 2 pairs)
- Flags observed: `table_names_residual`, `dash_one_suffix_residual`, `row_count_match`, `schema_subset`
```

`generated_at` は ISO-8601 with timezone (JST +09:00 推奨)。`overall_verdict` は太字、大文字 (`PASS` / `FAIL`)。

### ペアセクション

1 ペア = 1 セクション。テンプレート:

```markdown
## Pair 0: stockmarket_transaction_prepped → fct_transactions_summary

- Original: `0_Datasource / stockmarket_transaction_prepped` (LUID `<luid>`)
- New: `marts / fct_transactions_summary` (LUID `<luid>`, from flow `<flow-luid>`)
- **Verdict: FAIL**
- Flags: `table_names_residual`, `schema_subset`

### Schema diff

新側だけにある列 (1):

| 列名 | dataType | role |
|---|---|---|
| Table Names-1 | STRING | DIMENSION |

元側だけにある列: なし
dataType 不一致: なし
共通列数: 19

### Size diff

| 観点 | 元 | 新 | 完全一致? |
|---|---|---|---|
| 全体行数 | 45 | 102 | ❌ |

---
```

- ペア末尾には `---` (HR) を入れる
- セクション名は H2 `## Pair N: <original_name> → <new_name>`、サブセクションは H3
- 各リスト項目:
  - **Original** / **New** は project_name / DS name / LUID を 1 行で
  - **Verdict** は太字、大文字
  - **Flags** はバッククォート区切り。フラグが無いペアは行ごと省略
- 空のサブカテゴリ (「元のみに存在する列: なし」等) は行で明示する (テーブルを省略しない)
- 行数完全一致は `✅`、不一致は `❌`

## フラグ一覧

`comparison-report.md` のヘッダおよび各ペアの Flags 行に出現する文字列:

| フラグ名 | 由来 |
|---|---|
| `table_names_residual` | 新側スキーマに `Table Names` で始まる列がある |
| `dash_one_suffix_residual` | 新側スキーマに `-1` で終わる列がある |
| `row_count_match` | 全体行数が完全一致 |
| `schema_subset` | 元のみ列が空 (= 新が元を完全包含) |
| `schema_superset` | 新のみ列が空 (= 元が新を完全包含) |

## 判定基準

`SKILL.md §判定基準` 参照。要約:

- ペアの `verdict = pass` ⇔ schema diff が空 AND 全体行数が完全一致
- `overall_verdict = pass` ⇔ 全ペアが pass
