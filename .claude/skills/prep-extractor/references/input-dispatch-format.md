---
purpose: prep-extractor Phase B が出力する input-dispatch-mech.json の JSON スキーマ仕様。architect が consume する mechanical findings のフォーマット
note: トップレベル構造と per-Input record のスキーマ、kind 別の追加フィールド (pds / vconn / direct_db / extract) を規定する。policy 判定ルールは decomposition-plan-format.md に委譲
---

# input-dispatch-format

`work/<session>/reports/input-dispatch-mech.json` の JSON スキーマ。Phase B の `dispatch_inputs.py` が生成、prep-architect が読む。**ユーザー確認のための markdown ではない** — 単なる mechanical findings の永続化。

## 目次

- トップレベル構造
- per-Input record (共通フィールド / fields[] / kind 別固有フィールド: pds・vconn・direct_db・extract)
- consume 側の責務

## トップレベル構造

```jsonc
{
  "flow_path": "work/<session>/flow.json",
  "deploy_context_path": "work/<session>/reports/deploy-context.md",
  "input_count": 2,
  "kind_counts": {
    "pds": 1,
    "vconn": 1,
    "direct_db": 0,
    "extract": 0,
    "unknown": 0
  },
  "pds_project_parents_needed_in_scope": ["0_Datasource"],
  "inputs": [ /* per-input records, see below */ ]
}
```

`unknown` が 1 以上のとき dispatch_inputs.py は exit 2 で停止するため、本ファイルが書き出される時点で `kind_counts.unknown == 0` が保証される ([deploy-context-procedure.md §unknown 検出時の挙動](deploy-context-procedure.md))。

## per-Input record (`inputs[]` の各要素)

共通フィールド (全 kind):

| フィールド | 型 | 内容 |
|---|---|---|
| `node_id` | string | flow.json 内の Input ノード UUID |
| `node_name` | string | Tableau Prep UI 上の表示名 |
| `kind` | enum | `pds` / `vconn` / `direct_db` / `extract` |
| `node_type` | string | flow.json の nodeType (例: `.v2019_3_1.LoadSqlProxy`) |
| `fields` | array | 列メタの配列 (isGenerated=True 除外、下記スキーマ参照) |

### `fields[]` のスキーマ

```jsonc
{
  "name_raw": "9dc73cbd-8280-35c8-8406-cec646dcf77d",
  "name_bracketed": "[9dc73cbd-8280-35c8-8406-cec646dcf77d]",
  "caption": "数量",
  "datatype": "integer"
}
```

- `name_raw`: 列名 (uuid のことも raw 名のことも、Input によって異なる)
- `name_bracketed`: `[<name_raw>]` 形式。decomposition-plan の Transforms 表に `column_name` として転記される
- `caption`: ユーザー可読名 (Tableau Prep UI 上の名前、空文字列のこともある)
- `datatype`: `string` / `integer` / `real` / `date` / `datetime` / `bool` 等

### kind=pds 固有フィールド

```jsonc
"pds": {
  "project_name": "0_Datasource",
  "datasource_name": "stockmarket_data_prepped",
  "dbname": "stockmarket_data_prepped_17570800516990",
  "resolution": {
    "status": "resolved",
    "luid": "f1390b46-c0de-42e1-a470-974722e0800d",
    "project_path": "0_Datasource"
  }
}
```

`resolution.status` は 3 値:

| status | 追加フィールド | 意味 |
|---|---|---|
| `resolved` | `luid`, `project_path` | deploy-context.md で一意に LUID 解決 |
| `ambiguous` | `candidates: [{project_path, name, luid}, ...]` | 同名 PDS が複数候補。architect が Stop 2 でユーザー disambiguate |
| `unresolved` | `reason: string` | deploy-context.md に該当 PDS なし。architect は Phase B 再 scan or augment 切替を Stop 2 で提示 |

### kind=vconn 固有フィールド

```jsonc
"vconn": {
  "vconn_luid": "72b2ce16-b481-4088-9749-8a3593b92976",
  "vconn_caption": "Google Drive Tables",
  "table_uuid": "16bb67b3-17c6-4f8b-8e22-97f4dde8d16c",
  "table_name": "Transactions"
},
"augmenter_columns_hint": [
  {
    "name": "[e9143b20-3eb0-3e15-96fe-abc98655b63c]",
    "remote_name": "e9143b20-3eb0-3e15-96fe-abc98655b63c",
    "caption": "取引",
    "datatype": "string"
  },
  ...
]
```

`augmenter_columns_hint` は prep-pds-augmenter の vconn-source 入力フォーマットに整形済の列メタ。builder が `Materialization=live_pds` の stg を build するときにそのまま augmenter spec の `columns` フィールドに渡せる。

### kind=direct_db 固有フィールド

```jsonc
"direct_db": {
  "connection_class": "snowflake",
  "node_type": ".v1.LoadSql"
}
```

architect は `connection_class` を見て provisioning 案を組み立てる (snowflake → vconn 化 / extract → PDS publish 等)。

### kind=extract 固有フィールド

extract Input (local .hyper 等) は本リポでは実検証ゼロのため future schema。発生時は `extract` という kind ラベルのみ付与、追加情報は将来拡張で対応。architect は `needs_provisioning` として扱う。

## consume 側の責務

kind → policy の判定 (pds resolved → passthrough / vconn → augment + live_pds / direct_db・extract → needs_provisioning) と rename / provisioning の組み立てルールは architect 側の正典 [decomposition-plan-format.md §Input dispatch と stg materialization](../../../../references/decomposition-plan-format.md) に従う。本ファイルはスキーマのみを規定する。
