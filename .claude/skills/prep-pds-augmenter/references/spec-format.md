---
purpose: prep-pds-augmenter の spec.json 入力フォーマット仕様。source 3 種 (extract / live / vconn) ごとの必須/任意フィールド、transforms / calcs / 出力ファイル定義
note: SKILL.md からはこの reference にリンクし、本ファイルは spec の全フィールドを 1 箇所で網羅する
---

# spec.json フォーマット

`augment_pds.py --spec <path>` に渡す spec.json の全フィールド仕様。

## 目次

- 1 サイクルの単位 / 例 1 (kind: live) / 例 2 (kind: vconn)
- `source` / `target` / `mode`
- `source.columns[]` (kind=vconn 時のみ)
- `transforms[]` / `calcs[]`
- 出力

## 1 サイクルの単位

(source 1 個: PDS LUID または vconn 参照) + (transforms M 個) + (calc spec N 個) → (target PDS 1 個 publish)。

- extract/live: transforms と calcs のどちらか一方は非空必須 (両方空は validation error)
- vconn: 両方空も許容 (vconn テーブルをそのまま Live PDS として publish するパススルー用途)

## 例 1: 既存 Live PDS に対する stg 用 transforms + ad-hoc calc (`kind: live`)

```json
{
  "source": { "kind": "live", "luid": "<src-pds-luid>" },
  "target": { "project_id": "<target-project-luid>", "new_name": "stg_vconn__tableau_public" },
  "mode": "CreateNew",
  "transforms": [
    { "op": "rename", "column_name": "[<uuid>]", "to_caption": "workbook_repo_url" },
    { "op": "cast",   "column_name": "[<uuid>]", "to_caption": "view_count", "to_datatype": "real" },
    { "op": "hide",   "column_name": "[<uuid>]" }
  ],
  "calcs": [
    {
      "caption": "Profit Ratio",
      "formula": "SUM([Profit])/SUM([Sales])",
      "datatype": "real"
    }
  ]
}
```

## 例 2: vconn から base .tds をゼロから合成して publish (`kind: vconn`、既存 PDS なし)

```json
{
  "source": {
    "kind": "vconn",
    "vconn_luid": "<vconn-luid>",
    "vconn_caption": "Google Drive Tables",
    "table_uuid": "6a392323-6ff7-56e3-afa5-6bf35133447a",
    "table_name": "TableauPublic",
    "columns": [
      { "name": "[71773dea-8ab7-31a8-824e-1adaa86101a0]", "caption": "Workbook Repo Url", "datatype": "string" },
      { "name": "[10f83648-b8ca-3e2d-bb8b-bb11e8a1ea7d]", "caption": "View Count",        "datatype": "integer" }
    ]
  },
  "target": { "project_id": "<target-project-luid>", "new_name": "stg_vconn__tableau_public" },
  "mode": "CreateNew",
  "transforms": [
    { "op": "rename", "column_name": "[71773dea-8ab7-31a8-824e-1adaa86101a0]", "to_caption": "workbook_repo_url" },
    { "op": "cast",   "column_name": "[10f83648-b8ca-3e2d-bb8b-bb11e8a1ea7d]", "to_caption": "view_count", "to_datatype": "real" }
  ]
}
```

## `source` / `target` / `mode`

| フィールド | 必須 | 説明 |
|---|---|---|
| `source.kind` | no | `extract` (default) / `live` / `vconn` |
| `source.luid` | kind=extract/live 時必須 | 編集元 PDS LUID |
| `source.vconn_luid` | kind=vconn 時必須 | 合成元仮想接続の LUID |
| `source.vconn_caption` | kind=vconn 時推奨 | 仮想接続の display name (`<publishedConnection resourceName>` と `<named-connection caption>` に入る)。省略時は vconn_luid を流用 |
| `source.table_uuid` | kind=vconn 時必須 | vconn 内テーブルの UUID (`<relation table='[<uuid>].[<name>]'>` の前半) |
| `source.table_name` | kind=vconn 時必須 | vconn 内テーブル名 (`<relation table>` の後半・`<relation name>`) |
| `source.columns[]` | kind=vconn 時必須 | vconn テーブルの列メタを 1 列ずつ列挙 (詳細は下表)。auto-discovery しない |
| `target.project_id` | kind=vconn 時必須、他は no | 出力先 project LUID。extract/live は省略時 source PDS の project を継承、vconn は source PDS が無いので必須 |
| `target.new_name` | yes | 出力 PDS 名。`mode=Overwrite` では source の name と一致必須 |
| `mode` | no | `CreateNew` (default、vconn では唯一の選択肢) または `Overwrite` (extract/live のみ) |

## `source.columns[]` (kind=vconn 時のみ)

caller (= 通常 prep-builder) が vconn テーブルの列を 1 つずつ enumerate する。flow.json の Input ノードに列メタが揃っている前提。

| フィールド | 必須 | 説明 |
|---|---|---|
| `name` | yes | 内部 ID (bracket 込みの `[uuid-or-id]` 形式)。`<column name>` と `<metadata-record><local-name>` に入る |
| `caption` | yes | ユーザー可視名 (transform 前)。`<column caption>` と `<metadata-record><caption>` に入る |
| `datatype` | yes | `string` / `integer` / `real` / `date` / `datetime` / `boolean` |
| `remote_name` | no | `<metadata-record><remote-name>` / `<remote-alias>` に入る。省略時は `name` から brackets を外したもの |
| `role` | no | `dimension` / `measure`。datatype から導出 |
| `type` | no | `nominal` / `ordinal` / `quantitative`。role から導出 |
| `remote_type` | no | ODBC remote-type 番号。省略時は datatype から導出 (Cloud が publish 時に再 introspect するので best-effort) |
| `aggregation` | no | `<metadata-record><aggregation>`。省略時は datatype から導出 |

## `transforms[]` (column-level XML 操作)

`column_name` は元 `<column>` の `name` 属性 (内部 ID、bracket 込み)。caption ではなく内部名で参照する (rename で caption が変わっても安定なため)。

| op | 必須フィールド | 動作 |
|---|---|---|
| `rename` | `column_name`, `to_caption` | caption を書き換える。**semantics は source kind で異なる** (vconn = true rename / extract・live = caption-only、正典は [SKILL.md §rename semantics](../SKILL.md))。vconn の XML 実体 (local-name 書き換え + `<cols><map>`) は [tds-calc-field-format.md](tds-calc-field-format.md) |
| `cast` | `column_name`, `to_caption`, `to_datatype` | 元 column に `hidden='true'` を付け、cast calc を新規 column として注入。FUNC 導出表と XML 形は [tds-calc-field-format.md](tds-calc-field-format.md)。boolean は default なし、`cast_formula` で明示式が必要 |
| `hide` | `column_name` | `<column>` に `hidden='true'` を追加。VizQL field 一覧 / Workbook picker から消える (下流 Prep に対する遮蔽は未検証) |

`cast` のオプションフィールド: `cast_formula` (default の `FUNC(orig)` を上書き), `role` (default は to_datatype から導出), `type` (default は role から導出)。

## `calcs[]` (任意の派生列注入)

| フィールド | 必須 | 説明 |
|---|---|---|
| `caption` | yes | ユーザー可視 calc 名 |
| `formula` | yes | Tableau Calc 構文の式。caller 提供必須 |
| `datatype` | yes | `real` / `integer` / `string` / `boolean` / `date` / `datetime` |
| `role` | no | `measure` (default for numeric/datetime) / `dimension` |
| `type` | no | `quantitative` (default for measure) / `nominal` / `ordinal` |

## 出力

- Tableau Cloud に新規 (または上書き) PDS が publish される
- ローカル `<out-dir>/`:
  - `original.tdsx` — revert 用のオリジナル DL (vconn では合成された base .tdsx)
  - `original.tds` / `edited.tds` / `verified.tds` — 編集前後の比較用 XML
  - `edited.tdsx` — publish された .tdsx の現物
  - `verified.tdsx` — publish 後に再 DL した .tdsx
- stdout 最終行に `RESULT_JSON: {...}` を emit。フィールド: `published_luid` / `published_name` / `source_kind` / `transforms_applied` / `calcs_injected` (cast op が生成した synthetic calc も含む合計数) / `verified` / `transforms[]` / `calcs[]` / `next_step_recommendation`
