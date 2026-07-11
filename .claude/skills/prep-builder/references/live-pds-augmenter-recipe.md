---
purpose: prep-builder の Materialization=live_pds 分岐 — stg を .tfl でなく augmenter spec として emit する手順
note: plan の stg entry に Materialization=live_pds があるセッションでのみ読む。元 Input ノードの特定 / kind 再検証 / Transforms 表パース / spec.json 組み立てを規定。VConn Input を .tfl で組まざるを得ない場合の案内も含む
---

# live-pds-augmenter-recipe (Step 3-a)

stg entry の plan に `Materialization: live_pds` がある場合、.tfl を作らず [prep-pds-augmenter](../../prep-pds-augmenter/SKILL.md) の spec JSON を `flows/staging/<name>.augmenter.json` に書き出す。

## 目次

- 入力リソース / 元 Input ノードの特定 / kind 再検証 (silent fallback 禁止)
- Transforms 表のパース / spec.json の組み立て / 注意点
- VConn Input を .tfl で組まざるを得ない場合 — PDS 化を案内

## 入力リソース

- 元 flow.json (Step 2 の `original`)
- plan の当該 stg entry: `Inputs` (vconn caption + table 名)、`Transforms (column-level)` 表、`Outputs.Target project`
- `deploy-context.md`: stg datasources project の LUID

## 元 Input ノードの特定

plan の `Inputs` 行 (例: `Source: Google Drive Tables (仮想接続) / Transactions`) を参考に、元 flow.json から該当する Input ノードを探す:

```python
from flow_io import inspect_input_node, vconn_input_to_augmenter_columns

# 候補: baseType=input + (vconn caption が一致 or table 名が一致)
target_node_id = None
for nid, n in original["nodes"].items():
    if n.get("baseType") != "input":
        continue
    info = inspect_input_node(original, nid)
    if info["kind"] != "vconn":
        continue
    if info["vconn_caption"] == plan_input_vconn_caption and info["table_name"] == plan_input_table_name:
        target_node_id = nid
        info_for_spec = info
        break

if target_node_id is None:
    raise RuntimeError(
        f"plan の Inputs (vconn={plan_input_vconn_caption!r} / table={plan_input_table_name!r}) "
        f"に対応する vconn Input ノードが元 flow.json に見つからない。"
        f"plan の Materialization=live_pds 宣言が誤りか、元 flow が想定と違う。build を中断。"
    )
```

## kind 再検証 (silent fallback 禁止)

`inspect_input_node()` が `kind=vconn` を返さなければ即座に build 中断:

```python
if info_for_spec["kind"] != "vconn":
    raise RuntimeError(
        f"plan の Materialization=live_pds に対して元 Input ノード {target_node_id} の "
        f"kind={info_for_spec['kind']!r}。silent fallback せずに escalation。"
    )
```

## Transforms 表のパース

plan の `Transforms (column-level)` markdown 表を読み、augmenter spec の `transforms[]` 形式に変換。op 値が `rename` / `cast` / `hide` 以外なら build 中断 (decompose 設計エラー、architect 側 self-check 項目 13 で潰すべき項目):

```python
transforms = []
for row in parse_transforms_table(plan_md, stg_section_name):
    op = row["op"]
    t = {"op": op, "column_name": row["column_name"]}
    if op in ("rename", "cast"):
        t["to_caption"] = row["to_caption"]
    if op == "cast":
        t["to_datatype"] = row["to_datatype"]
    transforms.append(t)
```

## spec.json の組み立て

```python
columns = vconn_input_to_augmenter_columns(info_for_spec["fields"])

spec = {
    "source": {
        "kind": "vconn",
        "vconn_luid": info_for_spec["vconn_luid"],
        "vconn_caption": info_for_spec["vconn_caption"],
        "table_uuid": info_for_spec["table_uuid"],
        "table_name": info_for_spec["table_name"],
        "columns": columns,
    },
    "target": {
        "project_id": ctx["layer_projects"]["stg"]["ds_project"]["luid"],
        "new_name": stg_entry_name,
    },
    "mode": "CreateNew",
    "transforms": transforms,
}

spec_path = flows_dir / "staging" / f"{stg_entry_name}.augmenter.json"
spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
```

## 注意点

- **augmenter spec は .tfl と並列に `flows/staging/` 配下に置く**。拡張子 `.augmenter.json` で .tfl と区別。`publish_manifest.py init` がスキャンするのはこの命名規約に従う
- **vconn_input_to_augmenter_columns() は isGenerated=True のフィールドを除外**するので、Union 出力のような Tableau 注入列が augmenter spec に紛れることはない
- **column_name の bracket 形式は plan 側と Input ノード fields[] 側で一致必須** — plan に `[<uuid>]` で書いた column が `vconn_input_to_augmenter_columns()` の出力に存在しなければ augmenter 側で validation error になる (caller が ensure する責務)

## VConn Input を .tfl で組まざるを得ない場合 — PDS 化を案内

live_pds 経路を使わず、VConn Input (= `connections[].connectionAttributes.class == "publishedConnection"`) を持つ stg .tfl を組んでも、そのまま Cloud で動かないケースがある:

- VConn は背後の DB / Google Sheets / etc. を抽象化するため、`fields` の `name` は UUID で `caption` が実カラム名 (多言語含む)
- Cloud 上で flow を run すると VConn の現在のスキーマと .tfl 内 `fields` 配列が一致しないことがある (列追加/削除/型変更が VConn 側で起きていた場合)
- 後続ステップの calc が **存在しない列名** を参照するケースもあり、これは元 flow の編集時点のメタデータと現在の VConn スキーマの drift

**推奨対応**: VConn Input を検知したらユーザーに次のいずれかを案内する:

1. **(推奨) その VConn の出力を Tableau Cloud 上で一度 PDS 化してもらう** (Prep Builder GUI で VConn → PDS を吐き出す flow を 1 つ動かす)、その PDS を新しい stg flow の Input にする。以降 prep-builder は uniform に LoadSqlProxy + PDS で組める
2. (非推奨) VConn のまま使う — その場合は `connections` / `dataConnections` を元 .tfl からそのまま継承し、対象 site で同じ VConn が利用可能であることを事前確認。スキーマ drift が起きていれば手動修正が必要

VConn 入力 stg flow の自動ハンドリングはスコープ外。複雑性 (VConn の認証/権限/スキーマ管理) はユーザー判断に委ねる。
