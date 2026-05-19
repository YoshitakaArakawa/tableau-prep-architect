---
name: prep-output-comparator
description: 元フローの最終 Published DS と分解後フローの最終 Published DS を Tableau Metadata API + Tableau MCP で比較し、列差分と全体行数差分の機械的差分を Markdown レポートとして出力する Skill。prep-deployer の publish/run 完了後に「分解後 DS が元と等価か」の基礎的な parity チェックをしたいとき、ユーザーが「E2E 比較して」「元と新で差分を確認して」「parity チェックして」と発言したときに起動する。原因分析・修正提案・値そのものの比較は持たない (値同値性が必要なら caller が個別に query-datasource を叩くか、本 Skill を fork して拡張する)。修正判断はメインエージェントが Markdown を読んで prep-builder / prep-deployer の再呼び出しで対応する。
context: fork
agent: general-purpose
allowed-tools: Read Write Bash(python *) Glob Grep
---

# prep-output-comparator

元フローと分解後フローのそれぞれの最終 Published DS を比較し、**列差分** と **全体行数差分** のみを Markdown レポートとして出力する Skill。**読み取り専用** (書き込み副作用なし)。

スコープを基礎的なテストに絞っている理由: 業務知識なしに key column / measure column を自動選択すると、想定した分類列がスキーマに存在しないケース等で意味的に不適切な列にフォールバックし、結果の意味が壊れる。値そのものの比較が必要な場合は **caller が用途に応じて query-datasource を直接叩く** か、**ユーザーが本 Skill を fork して `measure_columns` 必須化版を作る** ことで、業務知識を caller / ユーザー側に明示的に置く。

役割対称性: 読み取り = prep-extractor + **prep-output-comparator** / 書き込み = prep-deployer。Cloud 上の DS を読むことだけが責務で、修正には踏み込まない。

## Caller から渡される入力

`context: fork` で動くため caller (メインエージェント) は会話履歴を渡せない。起動時に以下を文章で明示すること:

| 入力 | 必須 | 例 |
|---|---|---|
| `original_flow_luid` | ✅ | 元フローの LUID (1 個) |
| `new_flow_luids` | ✅ | 分解後フロー群の LUID 配列 (marts レイヤの .tfl)。**配列の順序が出力 PDS のペア順 (index pairing) になる**。Metadata API が原フローの outputSteps を返す順序とペアが一致しないとペアが意味的に逆転する。caller は decomposition-plan の名前対応を見て、原フローの output 順と一致するように並べて渡す責務がある |
| `output_dir` | ✅ | レポート出力先 (典型: `work/<yyyymmdd>_<tag>/reports/`)。MD は [CLAUDE.md §work/ ディレクトリ規約](../../../CLAUDE.md#work-ディレクトリ規約) の `reports/` に集約 |

flow LUID から output PDS への解決は本 Skill 内で行う ([scripts/resolve_pairs.py](scripts/resolve_pairs.py))。caller は **flow LUID だけ渡せばよく、PDS LUID を事前に解決する必要はない**。

key_columns / measure_columns / split_dimension の指定は受け付けない (auto-detect の沼を避けるため)。

## 出力

`output_dir` 配下に 1 ファイル:

- **comparison-report.md** — 人間 + メインエージェント LLM が読むための Markdown レポート (詳細は [references/report-format.md](references/report-format.md))

`pairs.json` (Step 1 のペア解決中間ファイル) も同じ directory に残してよい (デバッグ用)。

## ワークフロー

進捗:

- [ ] Step 1: ペア解決 (Metadata API)
- [ ] Step 2: スキーマ比較 (get-datasource-metadata)
- [ ] Step 3: 全体行数比較 (query-datasource: 全体 COUNT)
- [ ] Step 4: パターンフラグ検出
- [ ] Step 5: レポート出力 (Markdown)

### Step 1: ペア解決

[scripts/resolve_pairs.py](scripts/resolve_pairs.py) を実行:

```bash
python .claude/skills/prep-output-comparator/scripts/resolve_pairs.py \
  --original-flow-luid <luid> \
  --new-flow-luids <luid1> <luid2> ... \
  --output <output_dir>/pairs.json
```

`<output_dir>` は典型的には `work/<yyyymmdd>_<tag>/reports/`。

スクリプトは Tableau Metadata API (GraphQL) で各 flow の `downstreamDatasources` を辿り、新フロー群の output PDS を順次列挙する。元フローの出力 N 個と新フローの出力 N 個を **同じインデックス順** で並べたペアリストを `pairs.json` に書き出す。

**Caller 責務 (重要):** Metadata API が返す原フローの output 順と caller が渡した `new_flow_luids` の順序が一致していない場合、index pairing でペアが意味的に逆転する。caller は decomposition-plan の名前対応を読んで、原フローの output 順に合うように `new_flow_luids` を並べる責務がある。Skill 側では名前類似度による自動マッチは行わない (auto-detect の罠を避けるため)。元フローの output 数と渡された新フローの output 合計数が一致しない場合は警告のみ出して短い方で打ち切る。

### Step 2: スキーマ比較

各ペアに対し、Tableau MCP の `mcp__tableau__get-datasource-metadata` を **1 ペアあたり 2 回、ペア間は sequential** で叩く (並列は 401 になりやすい。詳細は [references/mcp-query-recipes.md](references/mcp-query-recipes.md))。以降本文では `get-datasource-metadata` / `query-datasource` / `list-datasources` と短縮表記する。

得られた field list から:

- 元のみに存在する列 (削除すべきでないものが消えた / リネームされた疑い)
- 新のみに存在する列 (削除漏れ / 重複 join の `-1` サフィックス / Union 暗黙注入の `Table Names-*` 等)
- 両方に存在するが dataType / role が異なる列

を抽出する。

### Step 3: 全体行数比較

各 DS で `query-datasource` を 1 回叩いて全体行数を取る:

```json
{ "fields": [{ "fieldCaption": "<最初の dimension 列>", "function": "COUNT", "fieldAlias": "row_count" }] }
```

dimension 列の選び方は [references/mcp-query-recipes.md](references/mcp-query-recipes.md) の「全体行数を取るレシピ」参照 (NULL を含まない列を選ぶ)。

元と新で全体行数が完全一致するかだけを判定する。元と分解後は同じソースデータから出ているので、本来一致するはず。**整数比較で完全一致のみを pass とする** (浮動小数の許容誤差は適用しない)。

### Step 4: パターンフラグ検出

機械的に判定できる「観察事実」のフラグを立てる (原因分析はしない):

| フラグ名 | 検出条件 |
|---|---|
| `table_names_residual` | 新側スキーマに `Table Names` で始まる列がある (`Table Names`, `Table Names-1`, ...) |
| `dash_one_suffix_residual` | 新側スキーマに `-1` で終わる列がある (`累計購入金額-1` 等) |
| `row_count_match` | 全体行数が完全一致 |
| `schema_subset` | 新のスキーマが元のスキーマを完全に包含 (新側だけ追加列がある状態) |
| `schema_superset` | 元のスキーマが新のスキーマを完全に包含 (新側で列が欠落) |

フラグの組み合わせは事実観察に留め、「これは namespace bug だ」のような原因解釈はレポートに書かない。

### Step 5: レポート出力

[references/report-format.md](references/report-format.md) の Markdown テンプレートに従い、`<output_dir>/comparison-report.md` を書く。各ペアごとに「スキーマ差分」「規模差分」「パターンフラグ」セクションを並べ、最後に **判定** (`pass` / `fail`) を 1 行。トップに `overall_verdict` を 1 行。

## 判定基準

ペアの `verdict`:

- `pass` — 列差分が空 (元のみ / 新のみ / dataType 不一致がすべて空) AND 全体行数が完全一致
- `fail` — 上記いずれかに違反

`overall_verdict`:

- `pass` — 全ペアが `pass`
- `fail` — 1 ペアでも `fail`

## 失敗時の動作

スクリプトや MCP 呼び出しが失敗した場合は **その時点で停止し、caller にエラーを返す** (autonomous-recovery はしない)。本 Skill は読み取り専用で副作用がないため、リトライは caller (メインエージェント) が判断する。

よくある失敗パターン:

- Metadata API: 該当 flow に downstreamDatasources がない / publishedDatasource ではなく Hyper 出力 → ペア解決不能。caller に「対象 flow が PDS を publish しているか確認してください」と返す
- MCP 401: 並列叩きで発生。sequential に切り替えれば解消 ([references/mcp-query-recipes.md](references/mcp-query-recipes.md))
- query-datasource: フィールド caption が一致しない (内部 ID と caption の混同) → スキーマから取った name をそのまま渡す
