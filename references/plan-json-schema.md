---
purpose: decomposition plan.json のスキーマ仕様 — prep-architect が emit し、render_plan_md.py が Stop 2 用 md をレンダリングし、prep-builder の build_from_plan.py が .tfl 群を機械組み立てする単一の設計成果物
note: トップレベル構造、flows[] entry (kind=tfl / pds_augment)、inputs[] の 3 種 (upstream_pds / passthrough_pds / transplant)、step 番号の解決規則を規定。md 側の見た目は decomposition-plan-format.md、検証・配線ロジックは scripts/plan_model.py が正典
---

# plan-json-schema

decompose フェーズの機械可読成果物 `decomposition-plan-<flow>.json` のスキーマ。**plan.json が設計の single source of truth** で、ユーザーレビュー用の `decomposition-plan-<flow>.md` は [render_plan_md.py](../.claude/skills/prep-architect/scripts/render_plan_md.py) が plan.json からレンダリングする (手書きしない)。build は [build_from_plan.py](../.claude/skills/prep-builder/scripts/build_from_plan.py) が plan.json だけを読む。

## 目次

- [step 番号の解決規則](#step-番号の解決規則)
- [トップレベル構造](#トップレベル構造)
- [flows[] entry: kind=pds_augment](#flows-entry-kindpds_augment)
- [flows[] entry: kind=tfl](#flows-entry-kindtfl)
- [inputs[] の 3 種](#inputs-の-3-種)
- [配線の導出規則 (エッジは書かない)](#配線の導出規則-エッジは書かない)
- [検証](#検証)

## step 番号の解決規則

plan.json はノードを **flow-summary.md の Topology 表と同じ step 番号** (1 始まり) で参照する。番号は `flow_io.bfs_order` (initialNodes からの BFS) で決まり、extractor / renderer / builder が同一実装を共有するため、decompose 時に書いた番号は build 時に同じノード UUID に解決される。`source.total_nodes` の一致チェックで「別フローの plan を適用した」事故を防ぐ。

## トップレベル構造

```jsonc
{
  "schema_version": "1",
  "flow_name": "stock-market-transaction-prep",   // flow-summary Meta の Flow name と一致
  "source": {
    "tfl_path": "work/<session>/<original>.tfl",  // 参考情報 (builder は --source 引数を使う)
    "total_nodes": 29                             // bfs_order 長との一致を build 時に検証
  },
  "server": { "url": "https://<pod>.online.tableau.com", "site_url_name": "<content-url>" },
  "original": {                                   // publish_manifest init --plan-json が使う
    "flow_luid": "<luid or null>",
    "outputs": [ { "name": "<original output PDS>", "luid": "<luid or null>" } ]
  },
  "flow_projects": {                              // .tfl publish 先 (deployer が使う)
    "staging":      { "path": "<target>/flows/stg",          "luid": "..." },
    "intermediate": { "path": "<target>/flows/intermediate", "luid": "..." },
    "marts":        { "path": "<target>/flows/marts",        "luid": "..." }
  },
  "ds_projects": {                                // PDS publish 先 (LSP Input / PublishExtract / augmenter target)
    "staging":      { "path": "<target>/datasources/stg",          "luid": "..." },
    "intermediate": { "path": "<target>/datasources/intermediate", "luid": "..." },
    "marts":        { "path": "<target>/datasources/marts",        "luid": "..." }
  },
  "flows": [ /* 下記 entry。名前は一意 */ ],
  "alternatives": [ { "title": "...", "body": "..." } ]   // optional (非自明な分岐のみ)
}
```

`server` / `flow_projects` / `ds_projects` / `original.outputs` は [gen_plan_skeleton.py](../.claude/skills/prep-architect/scripts/gen_plan_skeleton.py) が deploy-context.md / flow.json / input-dispatch-mech.json から機械生成する。architect が手で埋めるのは設計フィールドのみ。

## flows[] entry: kind=pds_augment

vconn Input を Live PDS 化する stg (staging のみ可)。.tfl は作られず `flows/staging/<name>.augmenter.json` が emit される。

```jsonc
{
  "name": "stg_gdrive__transactions",
  "layer": "staging",
  "kind": "pds_augment",
  "source_input_step": 2,                 // 元 flow の vconn Input の step 番号
  "table_name": "Transactions",           // optional: build 時の同一性チェック
  "transforms": [                         // op ∈ {rename, cast, hide}
    { "op": "rename", "column_name": "[<uuid>]", "to_caption": "quantity" },
    { "op": "cast",   "column_name": "[<uuid>]", "to_caption": "price", "to_datatype": "real" },
    { "op": "hide",   "column_name": "[<uuid>]" }
  ],
  "description": "1-2 行",
  "source_original_output_name": null     // stg が元 output を引き継ぐことは通常ない
}
```

`column_name` は input-dispatch-mech.json の `fields[].name_bracketed`。非 ASCII caption の `to_caption` は semantic translation ([decomposition-plan-format.md §Input dispatch](decomposition-plan-format.md))。

## flows[] entry: kind=tfl

```jsonc
{
  "name": "int_stock_trades",
  "layer": "intermediate",                // staging / intermediate / marts
  "kind": "tfl",
  "included_steps": [9, 10, 11],          // 丸ごと転写する元 step
  "splits": [                             // actions レベル分割 (このフローに残す slice)
    { "step": 4, "action_indices": [0, 1, 2, 3],
      "new_name": "Clean 1 (stg renames)", "note": "単純整形と Window 計算の混在" }
  ],
  "inputs": [ /* 下記 3 種 */ ],
  "output": { "name": "int_stock_trades" },  // PDS 名 = flow 名が規約。project は layer から導出
  "attach_output_to_step": null,          // optional: sink が複数のときだけ指定
  "joins": ["#9 SuperJoin orders×opps: cardinality `N:1` (補足)"],  // Join を含む場合必須
  "rename_back": [ { "from": "ticker", "to": "銘柄" } ],   // 元 output 引き継ぎ mart のみ
  "incremental": {                        // 元フローが incremental/append の場合のみ
    "input": "stg_gdrive__transactions",  // input の pds_name (transplant なら step 番号)
    "control_field": "Date", "output_field": "Date", "is_incremental_default": true
  },
  "source_original_output_name": "stockmarket_transaction_prepped",  // Output mapping 行
  "description": "1-2 行",
  "input_status": null                    // "needs_provisioning" なら build skip
                                          // (その場合 "provisioning": {source, kind, recommendation, resume} を付ける)
}
```

`included_steps` と `splits[].step` は排他 (split はそのフロー内で元 step を**置換**する)。同一 step の別 action slice を別フローに置くのが actions レベル分割の表現。

## inputs[] の 3 種

| kind | 用途 | フィールド |
|---|---|---|
| `upstream_pds` | **本 plan が生成する上流 PDS** を読む | `pds_name` (plan 内の flow 名と一致必須), `replaces_steps` |
| `passthrough_pds` | 元フローが読んでいた**既存 PDS** を新規 LSP で読む | `pds_name`, `project_path`, `luid`, `dbname` (推奨: input-dispatch の `pds.dbname`), `replaces_steps` |
| `transplant` | 元 Input ノードを**接続ごと verbatim 転写** (passthrough の第一選択。fields / dbname / Input renames を無変更で保持) | `step` |

**`replaces_steps` は「この入力が肩代わりする、元フローでの直接の親 step」** — 元 Input の step ではなく、このフローに含めた step を元フローで直接 feed していたノードの step を書く (例: 上流 stg フローの最終 Clean が #5 なら `[5]`)。namespace 継承 (Union/Join の入力識別) と lineage 検証の両方がこの値から導出される。同一 PDS を 2 系統で読む場合 (self-union 等) は inputs entry を 2 つに分ける。

## 配線の導出規則 (エッジは書かない)

plan.json にはエッジを書かない。[scripts/plan_model.py](../scripts/plan_model.py) の `compute_flow_graph` が導出する:

- included step 同士は元フローのエッジ (namespace verbatim) を保つ
- split は同フロー内で元 step の位置を引き継ぐ (親/子エッジを継承)
- **除外サブツリーへのエッジ**は前方探索で解決する: ①その先にこのフローのノードが 1 つだけ見つかれば **bridge** (間の除外 step をスキップ、namespace は最終ホップのものを継承 — 空 Clean の削除等)、②何も見つからなければ **drop** (他フローの領域 / 元 Output — fan-out 境界の正常系)、③複数見つかれば **error** (分岐 step をこのフローに含めて曖昧さを除去する)。bridge 経路上に合流点 (parents>1) の除外 step がある場合も error (片枝が silent に失われるため)
- 各 input は `replaces_steps` の子のうちこのフローに存在するものへ、元エッジの `nextNamespace` を継承して接続
- sink (出エッジ 0 のノード) が 1 つならそこへ Output (rename_back があれば間に挿入)。複数なら `attach_output_to_step` 必須

## 検証

`render_plan_md.py` (Stop 2 前) と `build_from_plan.py` (build 時) が同一の検証を実行する:

- 構造検証 (必須フィールド / kind / op / 参照整合)
- step 範囲・`source.total_nodes`・split の action_indices 範囲
- 配線可能性 (bridging 不能 / input が誰も feed しない / sink 曖昧)
- **lineage closure**: 全 included/split ノードが宣言 input から到達可能 (decompose-self-check の機械化部分)。build 後にはさらに `verify_lineage_closure` / `verify_edge_namespaces` (flow_io) が元フロー DAG との祖先関係を照合する (二重防御)

検証を通った plan だけが md にレンダリングされるため、**Stop 2 でユーザーが見る設計 = build される設計** が構造的に保証される。
