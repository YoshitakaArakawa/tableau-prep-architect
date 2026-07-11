---
name: prep-output-comparator
description: 元フローの最終 Published DS と分解後フローの最終 Published DS を Tableau Metadata API + Tableau MCP で比較し、列差分と全体行数差分の機械的差分を Markdown レポートとして出力する Skill。prep-deployer の publish/run 完了後に「分解後 DS が元と等価か」の基礎的な parity チェックをしたいとき、ユーザーが「E2E 比較して」「元と新で差分を確認して」「parity チェックして」と発言したときに起動する。原因分析・修正提案・値そのものの比較は持たない (値同値性が必要なら caller が個別に query-datasource を叩くか、本 Skill を fork して拡張する)。修正判断はメインエージェントが Markdown を読んで prep-builder / prep-deployer の再呼び出しで対応する。
context: fork
agent: general-purpose
model: claude-sonnet-5
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
| `manifest_path` | ✅ | session の `publish-manifest.json` のパス (典型: `work/<yyyymmdd>_<tag>/reports/publish-manifest.json`)。prep-deployer が `resolve-luids` まで完了した状態を前提とする。形式は [../../../references/publish-manifest-format.md](../../../references/publish-manifest-format.md) |
| `output_dir` | ✅ | レポート出力先 (典型: `work/<yyyymmdd>_<tag>/reports/`)。MD は [CLAUDE.md §work/ ディレクトリ規約](../../../CLAUDE.md#work-ディレクトリ規約) の `reports/` に集約 |
| `append_originals` | 元フローが append 出力のときのみ | `stockmarket_data_prepped の control field は Date` のように、**元 output PDS 名 → control field caption** の対応を文章で渡す。元フローの flow-summary.md の Meta (`Incremental inputs` / `Append-mode outputs`) と decomposition-plan の parity 検証方法 (self-check 項目 16) が情報源。指定されたペアは全体行数の一致判定を**期間一致カウント**に置き換える (Step 3 変形) |

ペア対応・LUID 解決はすべて manifest から取得する。caller が **個別の LUID 配列やペア順を組み立てる必要はない**。manifest の `decomposed_flows[].source_original_output_name` が原 PDS との対応の source of truth。

key_columns / measure_columns / split_dimension の指定は受け付けない (自動選択は業務知識なしには信頼できないため、本 Skill のスコープから外す)。

## 出力

`output_dir` 配下に 1 ファイル:

- **comparison-report.md** — 人間 + メインエージェント LLM が読むための Markdown レポート (詳細は [references/report-format.md](references/report-format.md))

`pairs.json` (Step 1 のペア解決中間ファイル) も同じ directory に残してよい (デバッグ用)。

メイン会話への戻り値の末尾に **`## Timing` ブロック** を必ず含める (フォーマットと Skill 別 breakdown 推奨項目: [skill-timing-contract.md](../../../references/skill-timing-contract.md))。

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
  --manifest <manifest_path> \
  --output <output_dir>/pairs.json
```

`<output_dir>` は典型的には `work/<yyyymmdd>_<tag>/reports/`。

スクリプトは manifest の `decomposed_flows[].source_original_output_name` を見て、対応する原 output PDS とのペアを組み、`original` / `new` 双方の LUID を manifest から引いて `pairs.json` に書き出す。`source_original_output_name = null` の decomposed flow (分解で新規生成された stg / 中間 PDS で、元フローの output と対応関係がない flow) はペア対象外で skip する。全層 PDS publish の前提でも、ペア対象は「元フローの output と対応する flow」に限定される。

manifest に LUID が null のまま残っているフィールドがあればエラーで止まる (prep-deployer の `resolve-luids` が未実行)。Metadata API への新規問い合わせは本スクリプトでは行わない (manifest が source of truth)。

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

**Step 3 変形 — `append_originals` に指定されたペア**: 元 output が append モード (過去 run の累積) の場合、全体行数の一致は原理的に成立しない。代わりに:

1. 新側で control field の `MIN`/`MAX` を取得
2. そのレンジで両側を `QUANTITATIVE_DATE` (または NUMERICAL) RANGE フィルタしてカウント
3. **期間内カウントの完全一致** を判定に使う。全体行数は両方とも取得してレポートに**参考値として記載** (不一致でも fail にしない)

クエリの具体形は [references/mcp-query-recipes.md §期間一致カウントのレシピ](references/mcp-query-recipes.md)。レポートには使用した control field とレンジ (min/max) を必ず記載する。

### Step 4: パターンフラグ検出

機械的に判定できる「観察事実」のフラグを立てる (原因分析はしない):

| フラグ名 | 検出条件 |
|---|---|
| `table_names_residual` | 新側スキーマに `Table Names` で始まる列がある (`Table Names`, `Table Names-1`, ...) |
| `dash_one_suffix_residual` | 新側スキーマに `-1` で終わる列がある (`累計購入金額-1` 等) |
| `row_count_match` | 全体行数が完全一致 (append ペアでは参考値) |
| `append_original` | caller が `append_originals` に指定したペア (全体行数比較は不成立、期間一致に切替済み) |
| `row_count_match_period` | control field レンジ内のカウントが完全一致 (append ペアのみ) |
| `schema_subset` | 新のスキーマが元のスキーマを完全に包含 (新側だけ追加列がある状態) |
| `schema_superset` | 元のスキーマが新のスキーマを完全に包含 (新側で列が欠落) |

フラグの組み合わせは事実観察に留め、「これは namespace bug だ」のような原因解釈はレポートに書かない。

### Step 5: レポート出力

[references/report-format.md](references/report-format.md) の Markdown テンプレートに従い、`<output_dir>/comparison-report.md` を書く。各ペアごとに「スキーマ差分」「規模差分」「パターンフラグ」セクションを並べ、最後に **判定** (`pass` / `fail`) を 1 行。トップに `overall_verdict` を 1 行。

## 判定基準

ペアの `verdict`:

- `pass` — 列差分が空 (元のみ / 新のみ / dataType 不一致がすべて空) AND 行数一致。行数一致の定義はペアの種別で切り替わる:
  - 通常ペア: **全体行数** が完全一致
  - `append_originals` 指定ペア: **control field レンジ内カウント** が完全一致 (全体行数は参考値、不一致でも fail にしない)
- `fail` — 上記いずれかに違反

**列比較は名前の厳密一致で行い、rename 対応付けによる救済はしない** (caller から「rename を考慮して対応付けよ」と指示されても適用しない)。分解側の規範として、元 output を引き継ぐ mart は rename-back で元列名に戻して publish される ([../../../references/decomposition-plan-format.md §Rename-back](../../../references/decomposition-plan-format.md)) ため、名前差分はそれ自体が gap であり、rename-back の取りこぼし検出こそ本 Skill の役割。

`overall_verdict`:

- `pass` — 全ペアが `pass`
- `fail` — 1 ペアでも `fail`

## 失敗時の動作

スクリプトや MCP 呼び出しが失敗した場合は **その時点で停止し、caller にエラーを返す** (autonomous-recovery はしない)。本 Skill は読み取り専用で副作用がないため、リトライは caller (メインエージェント) が判断する。

よくある失敗パターン:

- manifest が null LUID を含む: prep-deployer の `resolve-luids` が未実行。caller に「`python scripts/publish_manifest.py resolve-luids --manifest ...` を先に実行してください」と返す
- manifest に `source_original_output_name` を持つ decomposed flow が 0 件: marts レイヤの公開対象が無い (= decomposition-plan の Output mapping が空)。caller に decomposition-plan の Output mapping セクションを確認するよう案内
- MCP 401: 並列叩きで発生。sequential に切り替えれば解消 ([references/mcp-query-recipes.md](references/mcp-query-recipes.md))
- query-datasource: フィールド caption が一致しない (内部 ID と caption の混同) → スキーマから取った name をそのまま渡す
