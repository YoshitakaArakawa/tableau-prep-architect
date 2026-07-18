---
purpose: Tableau MCP (get-datasource-metadata / query-datasource) を tableau-pds-comparator から叩く際の癖と回避策
note: 本 Skill が実際に発行する MCP 呼び出し (get-datasource-metadata と全体行数 COUNT) に必要なレシピのみ。MCP 全般の API ドキュメント代わりではなく、本 Skill が引っかかった落とし穴のみを記載
---

# MCP Query Recipes

Tableau MCP を本 Skill から叩く際の運用上の癖と回避策。

## 目次

- 並列叩きで 401 が出る
- query-datasource はフィールド caption (内部 ID ではない)
- query-datasource のフィールド構造
- 全体行数を取るレシピ
- 期間一致カウントのレシピ (append 元出力向け)
- レスポンスの読み方

## 並列叩きで 401 が出る

Tableau MCP の `get-datasource-metadata` を **4 並列で叩くと、最初の 1 件は通るが残り 3 件が HTTP 401** で返ってくる症状がある。MCP サーバー側の認証セッション初期化レースだと思われる (最初の呼び出しで session を確立、確立完了前の後続が認証なしで叩いてしまう)。

**回避**: ペアごとに **sequential** で叩く。N ペアあるなら 2N 回の sequential 呼び出し (元 / 新 それぞれ 1 回ずつ)。

`list-datasources` と `query-datasource` も予防的に sequential を推奨。

なお本 Skill では LUID 解決は manifest で済むため `list-datasources` を叩く機会は基本ない。補助的に使う場合の注意 1 点のみ: `name:in:[...]` フィルタは parse エラーになる (実測)。`name:eq:<name>` を DS 数ぶん sequential で叩く。

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
{ "fieldCaption": "銘柄", "function": "COUNT", "fieldAlias": "row_count" }
```

本 Skill が `function` に使うのは `COUNT` (と append 用レシピの `MIN` / `MAX`) のみ。

## 全体行数を取るレシピ

「全行カウント」は専用の関数がない。NULL を含まない dimension 列を 1 つ選んで `COUNT` を掛ける:

```json
{
  "fields": [
    { "fieldCaption": "銘柄", "function": "COUNT", "fieldAlias": "row_count" }
  ]
}
```

dimension 列の選び方:

- `get-datasource-metadata` のレスポンスから `role == "DIMENSION"` の列を順に試す
- NULL を含む列を使うと NULL 行が落ちて実際より少ない値が返る
- 元 DS と新 DS で **同じ列名** を選ぶこと (両方に存在し、両方で NULL を含まない列)
- 適切な列が候補にない場合は `COUNTD` を全 dimension 列に掛けて多い側を採るより、caller にエラーを返す方が安全 (本 Skill は読み取り専用で、誤った行数を `pass` にして返すリスクの方が大きい)

## 期間一致カウントのレシピ (append 元出力向け)

元 output が append モード (過去 run の累積) の場合、全体行数は原理的に一致しない。代わりに **新側の control field の実レンジ** を取り、そのレンジ内で両側をカウントして比較する:

### Step 1: 新側の MIN/MAX を取る

```json
{
  "fields": [
    { "fieldCaption": "Date", "function": "MIN", "fieldAlias": "min_d" },
    { "fieldCaption": "Date", "function": "MAX", "fieldAlias": "max_d" }
  ]
}
```

### Step 2: 両側をそのレンジでフィルタしてカウント

```json
{
  "fields": [
    { "fieldCaption": "ID", "function": "COUNT", "fieldAlias": "row_count_period" }
  ],
  "filters": [
    {
      "field": { "fieldCaption": "Date" },
      "filterType": "QUANTITATIVE_DATE",
      "quantitativeFilterType": "RANGE",
      "minDate": "<min_d>",
      "maxDate": "<max_d>"
    }
  ]
}
```

注意:

- COUNT 対象列は全体行数レシピと同じ規準で選ぶ (両側に存在し NULL を含まない列)
- `minDate`/`maxDate` は Step 1 の値を ISO 形式 (`YYYY-MM-DD`) で渡す
- control field が date/datetime でない場合は `QUANTITATIVE_NUMERICAL` + `min`/`max` を使う
- **既知の限界**: 元側は append 時点のソーススナップショットの蓄積なので、その後ソースが過去日を改訂していた場合はレンジ内でも差が出る (これは分解の欠陥ではなくソース改訂。レポートには観察事実として両カウントとレンジを記載する)

## レスポンスの読み方

`query-datasource` のレスポンスは `{ "data": [{...row}, {...row}, ...] }` の形。`row` のキーは `fieldAlias` (指定した場合) または `fieldCaption` (デフォルト)。

全体行数を取るレシピでは 1 行のみが返る:

```json
{ "data": [
  { "row_count": 45 }
]}
```

NULL は JSON の `null` で返る。
