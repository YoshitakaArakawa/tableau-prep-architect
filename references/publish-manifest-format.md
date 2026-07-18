---
purpose: publish-manifest.json (セッション manifest) のスキーマ仕様
note: prep-builder が init し、prep-deployer が publish/run/resolve-luids で更新し、prep-output-comparator が読み取る。複数 Skill 共有の形式契約のためリポ直下に置く
---

# publish-manifest-format

`work/<yyyymmdd>_<tag>/reports/publish-manifest.json` のスキーマ。1 セッション内の **元フロー** と **分解後フロー群** の対応関係と publish/run 状態を集約する単一ファイル。

## 目次

- 役割と所有
- トップレベル構造
- フィールド仕様
- ライフサイクル
- decomposition-plan との接続
- repoint join model (旧→新 PDS 対応)
- 後方互換性

## 役割と所有

| Skill | 操作 |
|---|---|
| prep-architect | 直接書かない。plan.json の `flows[].source_original_output_name` で対応関係を表明 |
| prep-builder | **init**: `.tfl` 群 build 完了後に manifest を新規作成 (`--plan-json` + flows/ 直下のスキャンから組む。build_from_plan.py の `--manifest` で自動実行) |
| prep-deployer | **update-publish**: 各 .tfl を publish するたびに `decomposed_flows[].publish` を更新 / **update-run**: 各 run 後 `decomposed_flows[].run` を更新 / **resolve-luids**: chain 完了後 Metadata API で全 LUID を解決 |
| prep-output-comparator | **read-only**: ペア解決 (`scripts/resolve_pairs.py`) の唯一の入力 |
| prep-workbook-repointer / prep-pulse-repointer | **read-only**: design モードの repoint join (後述 §repoint join model) の RIGHT side |

CLI は repo 直下 [scripts/publish_manifest.py](../scripts/publish_manifest.py)。複数 Skill が共有するため repo 直下に置く (配置ルールは [../CLAUDE.md §Repo 構造](../CLAUDE.md#repo-構造) 参照)。

## トップレベル構造

```json
{
  "schema_version": "1",
  "generated_at": "2026-05-19T10:00:00+09:00",
  "session_work_dir": "work/20260517_stock-market-prep",
  "original": {
    "flow_name": "Stock Market Transaction Prep",
    "flow_luid": "aaaaaaaa-0000-0000-0000-000000000001",
    "outputs": [
      {
        "name": "stockmarket_transaction_prepped",
        "luid": "aaaaaaaa-0000-0000-0000-000000000002"
      },
      {
        "name": "stockmarket_transaction_detailed_prepped",
        "luid": "aaaaaaaa-0000-0000-0000-000000000003"
      }
    ]
  },
  "decomposed_flows": [
    {
      "name": "fct_transactions_summary",
      "layer": "marts",
      "kind": "tfl",
      "tfl_path": "flows/marts/fct_transactions_summary.tfl",
      "source_original_output_name": "stockmarket_transaction_prepped",
      "publish": {
        "status": "published",
        "flow_luid": "aaaaaaaa-0000-0000-0000-000000000004",
        "published_at": "2026-05-19T09:30:00+09:00"
      },
      "run": {
        "status": "success",
        "finish_code": 0,
        "run_at": "2026-05-19T09:35:00+09:00"
      },
      "outputs": [
        {
          "name": "fct_transactions_summary",
          "luid": "aaaaaaaa-0000-0000-0000-000000000005"
        }
      ]
    },
    {
      "name": "stg_vconn__transactions",
      "layer": "staging",
      "kind": "pds_augment",
      "augmenter_spec_path": "flows/staging/stg_vconn__transactions.augmenter.json",
      "source_original_output_name": null,
      "publish": {
        "status": "published",
        "pds_luid": "aaaaaaaa-0000-0000-0000-000000000006",
        "published_at": "2026-05-24T11:05:00+09:00"
      },
      "run": {
        "status": "n/a",
        "finish_code": null,
        "run_at": null
      },
      "outputs": [
        {
          "name": "stg_vconn__transactions",
          "luid": "aaaaaaaa-0000-0000-0000-000000000006"
        }
      ]
    }
  ]
}
```

## フィールド仕様

### トップレベル

| key | type | 必須 | 説明 |
|---|---|---|---|
| `schema_version` | string | ✅ | v1 では `"1"` 固定 |
| `generated_at` | ISO-8601 string | ✅ | 最終更新時刻 (init / update のたびに更新) |
| `session_work_dir` | string | ✅ | セッション作業ディレクトリの相対パス (`work/<yyyymmdd>_<tag>`) |
| `original` | object | ✅ | 元フロー (publish しない既存 flow) のメタ |
| `decomposed_flows` | array | ✅ | 分解後 flow 群 (全レイヤ stg/int/marts) |

### `original`

| key | type | 必須 | 説明 |
|---|---|---|---|
| `flow_name` | string | ✅ | flow-summary.md の `Flow name` |
| `flow_luid` | string \| null | init 時 null | session intake の Q1 から取得、もしくは `resolve-luids` で名前から逆引き |
| `outputs` | array of {`name`, `luid`} | ✅ | flow-summary.md の `Outputs:` から抽出した PDS 名リスト。`luid` は init 時 null、`resolve-luids` で埋まる |

### `decomposed_flows[]`

| key | type | 必須 | 説明 |
|---|---|---|---|
| `name` | string | ✅ | flow 名 = PDS 名 (本リポ規約)。`.tfl` 拡張子なし |
| `layer` | string | ✅ | `staging` / `intermediate` / `marts` |
| `kind` | string | ✅ | `tfl` (Prep flow を publish + run) または `pds_augment` (prep-pds-augmenter で Live PDS を直接 publish、run なし) |
| `tfl_path` | string | `kind=tfl` 時必須 | `session_work_dir` からの相対パス (例: `flows/marts/<name>.tfl`) |
| `augmenter_spec_path` | string | `kind=pds_augment` 時必須 | augmenter spec.json への相対パス (例: `flows/staging/<name>.augmenter.json`) |
| `source_original_output_name` | string \| null | ✅ | 元フローのどの output PDS に対応するか。元フローの output と対応関係のある flow (通常は marts、元が intermediate 相当の PDS を直接出していたケースは int も) は非 null、分解で新規生成された flow (stg / 中間 PDS) は null |
| `publish` | object | ✅ | 後述 (kind により形が変わる) |
| `run` | object | ✅ | 後述 (`kind=pds_augment` では常に `status=n/a`) |
| `outputs` | array of {`name`, `luid`} | ✅ | この flow の出力。`kind=tfl` は flow.json の PublishExtract から抽出、`kind=pds_augment` は augmenter spec の `target.new_name` 1 件。`luid` は `resolve-luids` で埋まる |

### `decomposed_flows[].publish`

`kind=tfl` のとき:

| key | type | 値 | 説明 |
|---|---|---|---|
| `status` | string | `pending` \| `published` \| `failed` | publish 状態 |
| `flow_luid` | string \| null | LUID | publish 成功時に Tableau Cloud 上の flow LUID |
| `published_at` | ISO-8601 \| null | timestamp | publish 成功時刻 |

`kind=pds_augment` のとき: `flow_luid` の代わりに `pds_luid` (publish された Live PDS の LUID) を持つ。`status` / `published_at` は共通。

### `decomposed_flows[].run`

`kind=tfl` のとき:

| key | type | 値 | 説明 |
|---|---|---|---|
| `status` | string | `pending` \| `success` \| `failed` \| `skipped` | run 状態。`skipped` は publish 失敗で run を試行しなかった場合 |
| `finish_code` | int \| null | 0 / 1 / 2 | Tableau Cloud の finishCode (0=Success, 1=Failed, 2=Cancelled)。run.status は finish_code から決定 |
| `run_at` | ISO-8601 \| null | timestamp | run 完了時刻 |

`kind=pds_augment` のとき: Live PDS は materialize する run フェーズを持たないため `status=n/a` 固定。`finish_code=null` / `run_at=null`。deployer はこの kind を見ると run 呼び出しを skip し、manifest を `status=n/a` のままにする。

## ライフサイクル

```
[prep-builder]            init                      → publish-manifest.json 生成
                            ↓ (全 flow status=pending, luid=null)
[prep-deployer publish]   update-publish (per .tfl) → publish.status / .flow_luid / .published_at セット
                            ↓ (publish 完了)
[prep-deployer run]       update-run (per flow)     → run.status / .finish_code / .run_at セット
                            ↓ (全レイヤ完走)
[prep-deployer]           resolve-luids             → original.flow_luid + 全 PDS LUID を Metadata API で解決
                            ↓
[prep-output-comparator]  read-only                 → ペア解決 → 比較
```

各更新コマンドは idempotent。途中 fail → 同じ command を再実行で OK。

## decomposition-plan との接続

plan.json ([plan-json-schema.md](plan-json-schema.md)) の `flows[].source_original_output_name` が `decomposed_flows[].source_original_output_name` の source of truth (`init --plan-json` が転記、`input_status=needs_provisioning` の entry は `publish.status=skipped_pending_provisioning` で登録)。md 側の `## Output mapping (original → decomposed)` 表は同フィールドのレンダリングで、旧セッション向け legacy `init` (`--decomposition-plan` + `--flow-summary`) でのみ直接パースされる。値を持たない flow (分解で新規生成された stg / 中間 PDS) は `source_original_output_name = null` で登録される。

## repoint join model (旧→新 PDS 対応)

prep-workbook-repointer / prep-pulse-repointer の design モードは、本 manifest を RIGHT side に旧→新 PDS 対応をローカル join で機械確定する。両 Skill の build plan スクリプト共通の正典:

1. inventory の旧 PDS `luid` を `original.outputs[].luid` と照合 → 旧 output `name` を得る
2. `decomposed_flows[]` で `source_original_output_name == 旧 output name` を探す → その `outputs[0]` が **新 PDS** (name / luid)

**主キーは luid**。`original.outputs[].luid` が null (resolve-luids 未実行) の場合は **PDS 名での fallback join** に切り替えて warning を立て、name で救済したペアは design.json の `match: "name"` で明示する (人間が対応の妥当性を確認できるように)。対応が確定できない旧 PDS は `unmapped_old_pds` に落とす — 移行対象外か、manifest の渡し漏れ / resolve-luids 未実行のサイン。

## 後方互換性

`schema_version` の bump 規律は、本 manifest を読み取る Skill / スクリプトが現リポ内 (prep-builder / prep-deployer / prep-output-comparator) に閉じている間は適用しない。外部から固定的に消費される public contract になった時点から bump 規律に切り替える。
