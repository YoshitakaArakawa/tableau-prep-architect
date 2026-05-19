---
purpose: publish-manifest.json (セッション manifest) のスキーマ仕様
fetched_at: 2026-05-19
note: prep-builder が init し、prep-deployer が publish/run/resolve-luids で更新し、prep-output-comparator が読み取る。複数 Skill 共有の形式契約のためリポ直下に置く
---

# publish-manifest-format

`work/<yyyymmdd>_<tag>/reports/publish-manifest.json` のスキーマ。1 セッション内の **元フロー** と **分解後フロー群** の対応関係と publish/run 状態を集約する単一ファイル。

## 役割と所有

| Skill | 操作 |
|---|---|
| prep-architect | 直接書かない。decomposition-plan の `## Output mapping` セクションで対応関係を表明 |
| prep-builder | **init**: `.tfl` 群 build 完了後に manifest を新規作成 (decomposition-plan の Output mapping + flow-summary + flows/ 直下のスキャンから組む) |
| prep-deployer | **update-publish**: 各 .tfl を publish するたびに `decomposed_flows[].publish` を更新 / **update-run**: 各 run 後 `decomposed_flows[].run` を更新 / **resolve-luids**: chain 完了後 Metadata API で全 LUID を解決 |
| prep-output-comparator | **read-only**: ペア解決 (`scripts/resolve_pairs.py`) の唯一の入力 |

CLI は repo 直下 [scripts/publish_manifest.py](../scripts/publish_manifest.py)。3 Skill が共有するため repo 直下に置く (詳細は [repo-layout.md](repo-layout.md))。

## トップレベル構造

```json
{
  "schema_version": "1",
  "generated_at": "2026-05-19T10:00:00+09:00",
  "session_work_dir": "work/20260517_stock-market-prep",
  "original": {
    "flow_name": "Stock Market Transaction Prep",
    "flow_luid": "84d06013-e571-49c2-b950-233e4cb36933",
    "outputs": [
      {
        "name": "stockmarket_transaction_prepped",
        "luid": "fcaaa8ec-786b-4be8-bb34-aba29bdb0cbb"
      },
      {
        "name": "stockmarket_transaction_detailed_prepped",
        "luid": "5d17b2e3-bfc6-4299-8a35-7d4e843b47c2"
      }
    ]
  },
  "decomposed_flows": [
    {
      "name": "fct_transactions_summary",
      "layer": "marts",
      "tfl_path": "flows/marts/fct_transactions_summary.tfl",
      "source_original_output_name": "stockmarket_transaction_prepped",
      "publish": {
        "status": "published",
        "flow_luid": "a740445c-eed7-4f10-a5f7-ebdc507f1d80",
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
          "luid": "474dbbf2-0e48-4013-85cd-225ea9ce074c"
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
| `name` | string | ✅ | .tfl ファイル名 (拡張子なし) = flow 名 = PDS 名 (本リポ規約) |
| `layer` | string | ✅ | `staging` / `intermediate` / `marts` |
| `tfl_path` | string | ✅ | `session_work_dir` からの相対パス (例: `flows/marts/<name>.tfl`) |
| `source_original_output_name` | string \| null | ✅ | 元フローのどの output PDS に対応するか。marts レイヤの output flow のみ非 null、stg/int (Hyper のみ) は null |
| `publish` | object | ✅ | 後述 |
| `run` | object | ✅ | 後述 |
| `outputs` | array of {`name`, `luid`} | ✅ | この flow の PublishExtract 出力 (init 時 flow.json から抽出、複数あり得る)。Hyper のみの flow なら空配列。`luid` は `resolve-luids` で埋まる |

### `decomposed_flows[].publish`

| key | type | 値 | 説明 |
|---|---|---|---|
| `status` | string | `pending` \| `published` \| `failed` | publish 状態 |
| `flow_luid` | string \| null | LUID | publish 成功時に Tableau Cloud 上の flow LUID |
| `published_at` | ISO-8601 \| null | timestamp | publish 成功時刻 |

### `decomposed_flows[].run`

| key | type | 値 | 説明 |
|---|---|---|---|
| `status` | string | `pending` \| `success` \| `failed` \| `skipped` | run 状態。`skipped` は publish 失敗で run を試行しなかった場合 |
| `finish_code` | int \| null | 0 / 1 / 2 | Tableau Cloud の finishCode (0=Success, 1=Failed, 2=Cancelled)。run.status は finish_code から決定 |
| `run_at` | ISO-8601 \| null | timestamp | run 完了時刻 |

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

prep-architect の decomposition-plan-<flow>.md に **必須セクション** `## Output mapping (original → decomposed)` を追加する (詳細は [decomposition-plan-format.md](decomposition-plan-format.md))。本セクションの Markdown 表が `decomposed_flows[].source_original_output_name` の source of truth。

形式:

```markdown
## Output mapping (original → decomposed)

| Original output PDS | Decomposed flow | Decomposed output PDS |
|---|---|---|
| stockmarket_transaction_prepped | fct_transactions_summary | fct_transactions_summary |
| stockmarket_transaction_detailed_prepped | fct_transactions_matched | fct_transactions_matched |
```

- 元 PDS が複数の decomposed flow に分かれる場合は 1 元 PDS = 複数行で表現
- 元 PDS と対応関係が無い stg/int flow は本表に出さない (`source_original_output_name = null` で manifest に登録)
- 表の `Decomposed output PDS` は publish 後の PDS 名で、`Decomposed flow` の flow 名と一致するのが本リポ規約だが、別名にしたい場合は両方記載 (`outputs[].name` に転記される)

## 後方互換性

v1 は PoC レベルでリリース前のため、フォーマット変更は `schema_version` を上げずに行う。v1 完成後 (= 後段 Skill が manifest を本番消費するようになって以降) は schema 変更時に bump。
