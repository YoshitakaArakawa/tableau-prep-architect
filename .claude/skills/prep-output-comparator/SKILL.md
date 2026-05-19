---
name: prep-output-comparator
description: 元フローの最終 Published DS と分解後フローの最終 Published DS を Tableau Metadata API + Tableau MCP で比較し、スキーマ / 行数 / 値の機械的差分を構造化レポート (Markdown + JSON) として出力する Skill。prep-deployer の publish/run 完了後に「分解後 DS が元と等価か」を検証したいとき、ユーザーが「E2E 比較して」「元と新で差分を確認して」「parity チェックして」と発言したときに起動する。原因分析や修正提案は持たず、事実差分のみを出力する。修正判断はメインエージェントが Markdown / JSON を読んで prep-builder / prep-deployer の再呼び出しで対応する。
context: fork
agent: general-purpose
allowed-tools: Read Write Bash(python *) Glob Grep
---

# prep-output-comparator

元フローと分解後フローのそれぞれの最終 Published DS を比較し、**機械的差分** (列差分 / 行数差分 / 主要 measure 差分) を構造化レポートとして出力する Skill。**読み取り専用** (書き込み副作用なし)。

役割対称性: 読み取り = prep-extractor + **prep-output-comparator** / 書き込み = prep-deployer。Cloud 上の DS を読むことだけが責務で、修正には踏み込まない。

## Caller から渡される入力

`context: fork` で動くため caller (メインエージェント) は会話履歴を渡せない。起動時に以下を文章で明示すること:

| 入力 | 必須 | 例 |
|---|---|---|
| `original_flow_luid` | ✅ | 元フローの LUID (1 個) |
| `new_flow_luids` | ✅ | 分解後フロー群の LUID 配列 (marts レイヤの .tfl) |
| `output_dir` | ✅ | レポート出力先 (典型: `work/<yyyymmdd>_<tag>/reports/`)。MD/JSON は [CLAUDE.md §work/ ディレクトリ規約](../../../CLAUDE.md#work-ディレクトリ規約) の `reports/` に集約 |
| `key_columns` | 任意 | 行数を分割するキー列名のリスト (例: `["銘柄"]`)。未指定なら全体行数のみ |
| `measure_columns` | 任意 | SUM/MIN/MAX を取る measure 列名のリスト。未指定ならスキーマから REAL/INTEGER の MEASURE 上位 5 列を自動選択 |

flow LUID から output PDS への解決は本 Skill 内で行う ([scripts/resolve_pairs.py](scripts/resolve_pairs.py))。caller は **flow LUID だけ渡せばよく、PDS LUID を事前に解決する必要はない**。

## 出力

`output_dir` 配下に 2 ファイル:

- **comparison-report.md** — 人間が読むための Markdown レポート (詳細は [references/report-format.md](references/report-format.md))
- **comparison-report.json** — 機械可読の構造化レポート (同上、後段判断に消費される)

両ファイルは同じ事実を別フォーマットで表現する。JSON は **比較観点が固定された API contract** として扱い、本 Skill の v1 ではスキーマを固定する。

## ワークフロー

進捗:

- [ ] Step 1: ペア解決 (Metadata API)
- [ ] Step 2: スキーマ比較 (get-datasource-metadata)
- [ ] Step 3: 規模比較 (query-datasource: 全体 COUNT + key 別 COUNT)
- [ ] Step 4: 値比較 (query-datasource: 主要 measure の SUM)
- [ ] Step 5: パターンフラグ検出
- [ ] Step 6: レポート出力 (Markdown + JSON)

### Step 1: ペア解決

[scripts/resolve_pairs.py](scripts/resolve_pairs.py) を実行:

```bash
python .claude/skills/prep-output-comparator/scripts/resolve_pairs.py \
  --original-flow-luid <luid> \
  --new-flow-luids <luid1> <luid2> ... \
  --output <output_dir>/pairs.json
```

`<output_dir>` は典型的には `work/<yyyymmdd>_<tag>/reports/`。

スクリプトは Tableau Metadata API (GraphQL) で各 flow の `outputSteps` → `publishedDatasource` を辿り、新フロー群の output PDS を順次列挙する。元フローの出力 N 個と新フローの出力 N 個を **同じインデックス順** で並べたペアリストを `pairs.json` に書き出す。

ペア対応が一意でない場合 (元 1 → 新 2 のような fan-out) はスクリプトが警告を出し、caller の確認を促す。

### Step 2: スキーマ比較

各ペアに対し、Tableau MCP の `mcp__tableau__get-datasource-metadata` を **1 ペアあたり 2 回、ペア間は sequential** で叩く (並列は 401 になりやすい。詳細は [references/mcp-query-recipes.md](references/mcp-query-recipes.md))。以降本文では `get-datasource-metadata` / `query-datasource` / `list-datasources` と短縮表記する。

得られた field list から:

- 元のみに存在する列 (削除すべきでないものが消えた / リネームされた疑い)
- 新のみに存在する列 (削除漏れ / 重複 join の `-1` サフィックス / Union 暗黙注入の `Table Names-*` 等)
- 両方に存在するが dataType / role が異なる列

を抽出する。

### Step 3: 規模比較

各 DS で `query-datasource` を叩いて:

1. **全体行数**: `COUNT(<最初の dimension 列>)` で全体行数
2. **キー別行数** (`key_columns` 指定時のみ): キー列を dimension、COUNT を measure として取得

元と新で全体行数の比率を計算。キー別なら各キー値での比率も計算。

### Step 4: 値比較

`measure_columns` で指定された (または自動選択された) measure 列について、`query-datasource` で **`取引` 等の dimension で分割した SUM** を取得する。

`measure_columns` が未指定の場合の自動選択ルール:

- 元 DS のスキーマから `role == "MEASURE"` かつ `dataType in ["REAL", "INTEGER"]` の列を取得
- そのうち列名が `-1` で終わるもの・`row_num` 系の内部ナンバリングは除外
- 残りの先頭 5 列まで

### Step 5: パターンフラグ検出

機械的に判定できる「観察事実」のフラグを立てる (原因分析はしない):

| フラグ名 | 検出条件 |
|---|---|
| `clean_2x_multiple` | 全 measure の (新 SUM / 元 SUM) がほぼ ×2.00 (誤差 ±1%) |
| `clean_3x_multiple` | 同上、×3.00 |
| `table_names_residual` | 新側スキーマに `Table Names` で始まる列がある (`Table Names`, `Table Names-1`, ...) |
| `dash_one_suffix_residual` | 新側スキーマに `-1` で終わる列がある (`累計購入金額-1` 等) |
| `row_count_match` | 全体行数が完全一致 |
| `schema_subset` | 新のスキーマが元のスキーマを完全に包含 (新側だけ追加列がある状態) |
| `schema_superset` | 元のスキーマが新のスキーマを完全に包含 (新側で列が欠落) |

フラグの組み合わせは事実観察に留め、「これは namespace bug だ」のような原因解釈はレポートに書かない。

### Step 6: レポート出力

[references/report-format.md](references/report-format.md) の仕様に従い、以下 2 ファイルを `output_dir` に書く:

- `comparison-report.md` — 各ペアごとに「スキーマ差分」「規模差分」「値差分」「パターンフラグ」セクションを並べ、最後に **判定** (`pass` / `fail`) を 1 行
- `comparison-report.json` — 同じ情報を JSON で

JSON のトップレベル構造:

```json
{
  "generated_at": "<ISO-8601>",
  "original_flow_luid": "...",
  "new_flow_luids": ["...", "..."],
  "pairs": [
    {
      "pair_index": 0,
      "original": {"luid": "...", "name": "...", "project": "..."},
      "new":      {"luid": "...", "name": "...", "project": "..."},
      "schema_diff": {...},
      "size_diff":   {...},
      "value_diff":  {...},
      "flags":       ["clean_2x_multiple", "table_names_residual"],
      "verdict":     "fail"
    }
  ],
  "overall_verdict": "fail"
}
```

詳細スキーマは [references/report-format.md](references/report-format.md)。

## 判定基準

ペアの `verdict`:

- `pass` — `schema_diff` が空 AND 全体行数比率が 1.00 ±1% AND 全 measure の SUM 比率が 1.00 ±1%
- `fail` — 上記いずれかに違反

`overall_verdict`:

- `pass` — 全ペアが `pass`
- `fail` — 1 ペアでも `fail`

許容誤差 ±1% は浮動小数演算の許容範囲。Tableau 内部で USD→JPY のような掛け算が入る場合の累積誤差を吸収する想定。

## 失敗時の動作

スクリプトや MCP 呼び出しが失敗した場合は **その時点で停止し、caller にエラーを返す** (autonomous-recovery はしない)。本 Skill は読み取り専用で副作用がないため、リトライは caller (メインエージェント) が判断する。

よくある失敗パターン:

- Metadata API: 該当 flow に outputSteps がない / publishedDatasource ではなく Hyper 出力 → ペア解決不能。caller に「対象 flow が PDS を publish しているか確認してください」と返す
- MCP 401: 並列叩きで発生。sequential に切り替えれば解消 ([references/mcp-query-recipes.md](references/mcp-query-recipes.md))
- query-datasource: フィールド caption が一致しない (内部 ID と caption の混同) → スキーマから取った name をそのまま渡す

## 関連 Skill

- [prep-extractor](../prep-extractor/SKILL.md) — Cloud 読み取りの先輩 Skill。役割対称性で本 Skill と並ぶ
- [prep-deployer](../prep-deployer/SKILL.md) — 本 Skill の前段 (publish/run を完了させる)
- [prep-builder](../prep-builder/SKILL.md) — 本 Skill のレポートを受けて修正 .tfl を再構築 (メインエージェントが判断して呼ぶ)
