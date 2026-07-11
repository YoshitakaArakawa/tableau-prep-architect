---
purpose: prep-builder が新規 .tfl ファイル群を生成するための具体手順
fetched_at: 2026-05-17
note: 設計案パース → 元 .tfl 展開 → ノード抽出・接続再構成 → zip 化までの 5 ステップとエラーハンドリング・検証手順を規定
---

# build-recipe

`prep-builder` Skill が行う **Build フェーズ** の具体手順。prep-architect の decompose 設計案に従って、新規 .tfl ファイル群を生成するワークフロー。

## 大原則

1. **元 .tfl は絶対に変更しない** — 新規ファイルとして書き出す
2. **既存 `flows/` 配下の同名ファイルは上書きしない** — 警告してユーザーに確認
3. **生成した各 .tfl は Tableau Prep Builder で単体動作可能であること**
4. **失敗したらその .tfl の生成を中断、ユーザーに報告**（自動回避しない）
5. **build 前後で必ず Lineage closure check を回す** ([scripts/flow_io.py](../../../../scripts/flow_io.py) の `verify_lineage_closure`)。decompose の取り違え (= ステップを誤ったレイヤ / 誤った Input branch に配置) を機械的に検知

## 入力

- prep-architect の decompose の出力（`decomposition-plan-<flow>.md`、書式は [../../../../references/decomposition-plan-format.md](../../../../references/decomposition-plan-format.md)）
- 元の .tfl / .tflx（ノード定義の抽出元）
- ユーザー作業フォルダのパス（`flows/` を作る場所）

## 手順概要

```
1. 設計案 markdown をパース
   ↓
2. 元 .tfl を unzip → flow JSON を取得
   ↓
3. 各 plan entry を stg dispatch で振り分け:
   - input_status=needs_provisioning (direct_db / extract) → build を skip
     a. manifest に status=skipped_pending_provisioning + 整備依頼 (plan の `## Input provisioning required` から転記) を記録
     b. ファイル成果物は出さない (.tfl も .augmenter.json も)
     c. 下流 int/marts はそのまま build (run 時に該当 stg PDS 不在で fail する想定、正常な escalation 経路)
   - Materialization=live_pds (stg のみ) → augmenter spec を emit
     a. plan の Inputs で参照されている vconn と table 名から、元 flow.json の対応 Input ノードを特定
     b. inspect_input_node() で kind=vconn を再検証 (それ以外なら build 中断)
     c. Transforms (column-level) 表の op 値が rename/cast/hide のみか検証 (混入していれば build 中断、decompose 設計エラー)
     d. vconn_input_to_augmenter_columns() で列メタを取得
     e. plan の Transforms (column-level) 表と組合せて augmenter spec を JSON 出力
     f. flows/staging/<name>.augmenter.json に書き出す
   - それ以外 (Materialization=tfl + int + marts) → .tfl を組む:
     a. 設計案から「含めるべき元ステップ ID」を取得
     b. 元 JSON から該当ノード＋接続定義を抽出
     c. 切れた依存（前段が別 .tfl になる）は新 LoadSqlProxy Input ノードに置き換え
     d. 設計案に actions 単位の分割指示があれば SuperTransform の beforeActionAnnotations を振り分け
     e. 末端ノードに新しい Output ノード (PublishExtract) を追加
     f. 新 flow JSON を組み立て
     g. zip 化して .tfl として保存
   ↓
4. 生成サマリをユーザーに報告 (kind=tfl と kind=pds_augment の件数、および skipped_pending_provisioning の件数を別途)
```

## Step 3-pre: int / marts entry の Inputs 解決 (passthrough 対応)

int / marts の `Inputs` リストには 2 種類の PDS 参照が混在しうる:

1. **decompose 生成済 stg PDS** — `Published DS: stg_<name>` のように decompose で plan に並べた stg entry の名前を参照。`deploy-context.md` の datasources 配下 (`<target>/datasources/stg/<name>`) で publish される前提
2. **passthrough from source flow** — `Published DS: <source-pds-name>` + `Project path: <path>` + `LUID: <luid>` の 3 行で記述された、元 flow から直接引き継ぐ PDS

builder は両方を **同じ LoadSqlProxy ノード** で表現する (`flow_io.add_pds_input` 経由)。違いは Server 上の存在保証だけ:

- decompose 生成済 stg PDS: prep-deployer が同セッション内で publish する (まだ存在しない or 古い)。build 時点では存在チェック不可
- passthrough PDS: architect Stop 2 確認時点で既存。build 時に `deploy-context.md` から LUID を引いて実在検証可能

実装:

```python
from flow_io import add_pds_input, wire_new_input_to_child

ctx = parse_deploy_context("deploy-context.md")  # server_url, site_url_name, layer LUIDs

# Case 1: decompose 生成済 stg PDS (例 stg_snowflake__orders)
lsp_id, _ = add_pds_input(
    new_flow,
    server_url=ctx["server_url"],
    site_url_name=ctx["site_url_name"],
    project_name=ctx["layer_projects"]["stg"]["ds_project"]["name"],
    datasource_name="stg_snowflake__orders",
    dbname=None,
    fields=[],
    next_nodes=None,
)

# Case 2: passthrough source PDS (例 stockmarket_data_prepped from 0_Datasource)
# plan entry の Inputs 行から project_path / name / LUID を抽出済の前提。
# project_name は path の末端 (leaf) を使う。LoadSqlProxy の connectionAttributes に
# 入る projectName は leaf 名 (元 .tfl がそうなっている)。
lsp_id, _ = add_pds_input(
    new_flow,
    server_url=ctx["server_url"],
    site_url_name=ctx["site_url_name"],
    project_name="0_Datasource",                    # plan の Project path の leaf
    datasource_name="stockmarket_data_prepped",
    dbname=None,                                    # passthrough でも build 時は None で OK
    fields=[],
    next_nodes=None,
)
```

`wire_new_input_to_child` 部分は両ケース共通 (元 .tfl の対応する parent 関係から `replaced_source_parent_id` を解決)。

**build 時の実在検証** (passthrough のみ): plan に明記された LUID が `deploy-context.md` の Datasources in scope に存在するか grep で確認。不在なら build 中断 (plan が古い or Phase B の `--also-scan` が足りない)。

> ⚠️ passthrough Input の場合、`projectName` (LoadSqlProxy の connectionAttributes) はテーブル内部の **leaf 名** が使われる。元 flow の Input ノードが `0_Datasource/foo/bar` のような深い path にあっても、connectionAttributes の `projectName` は `bar` (leaf) だけが格納される (Tableau の挙動)。LUID 解決は project_path 全体で行い、Server 接続情報には leaf 名を使う、という非対称性に注意。

## Step 3-a: Materialization=live_pds の augmenter spec 組み立て

stg entry の plan に `Materialization: live_pds` がある場合の手順。

### 入力リソース

- 元 flow.json (Step 2 の `original`)
- plan の当該 stg entry: `Inputs` (vconn caption + table 名)、`Transforms (column-level)` 表、`Outputs.Target project`
- `deploy-context.md`: stg datasources project の LUID

### 元 Input ノードの特定

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

### kind 再検証 (silent fallback 禁止)

`inspect_input_node()` が `kind=vconn` を返さなければ即座に build 中断:

```python
if info_for_spec["kind"] != "vconn":
    raise RuntimeError(
        f"plan の Materialization=live_pds に対して元 Input ノード {target_node_id} の "
        f"kind={info_for_spec['kind']!r}。silent fallback せずに escalation。"
    )
```

### Transforms 表のパース

plan の `Transforms (column-level)` markdown 表を読み、augmenter spec の `transforms[]` 形式に変換:

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

### spec.json の組み立て

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

### 注意点

- **augmenter spec は .tfl と並列に `flows/staging/` 配下に置く**。拡張子 `.augmenter.json` で .tfl と区別。`publish_manifest.py init` がスキャンするのはこの命名規約に従う
- **vconn_input_to_augmenter_columns() は isGenerated=True のフィールドを除外**するので、Union 出力のような Tableau 注入列が augmenter spec に紛れることはない
- **column_name の bracket 形式は plan 側と Input ノード fields[] 側で一致必須** — plan に `[<uuid>]` で書いた column が `vconn_input_to_augmenter_columns()` の出力に存在しなければ augmenter 側で validation error になる (caller が ensure する責務)

## ステップ詳細

### Step 1: 設計案パース

decomposition-plan markdown から以下を抽出:

- 各 `### <flow-name>` セクションの `Included original steps` の番号リスト
- `Layer` から出力ディレクトリ（`flows/staging/` / `flows/intermediate/` / `flows/marts/`）を決定
- `Inputs` / `Outputs` から接続定義を決定
- `## Actions-level splits` から SuperTransform 分割指示を取り出す

### Step 2: 元 .tfl の展開

Repo 直下 [scripts/flow_io.py](../../../../scripts/flow_io.py) を使う:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from flow_io import load_flow_json, load_aux_entries, PUBLISHABLE_AUX_ENTRIES

original = load_flow_json(src_path)

# publish 必須の zip entry (maestroMetadata, displaySettings) を元 .tfl から確保
aux_all = load_aux_entries(src_path)
aux_for_new = {k: aux_all[k] for k in PUBLISHABLE_AUX_ENTRIES if k in aux_all}
```

**Container 形式の正規化 (必須・冪等)**: フローによっては Clean ステップが `.v1.Container` で表現される。読み込み直後に 1 度だけ正規化して以後の全処理を flat SuperTransform 形式に統一する。node id は保たれるので設計案の step ID 参照はそのまま解決する。詳細は [../../../../references/tfl-json-schema.md §Clean ステップの 2 つのシリアライズ形式](../../../../references/tfl-json-schema.md#clean-ステップの-2-つのシリアライズ形式)。

```python
from flow_io import normalize_source_containers

original, skipped = normalize_source_containers(original)
if skipped:
    # 非変換 Container (マルチ namespace / 分岐 / ネスト): verbatim 転写のみ可、actions 分割不可
    print(f"[warn] non-convertible containers left verbatim: {skipped}")
```

Container を含まない (既に flat) フローでは no-op (`skipped=[]`, ノード無変更) なので無条件に呼んでよい。

`original["nodes"]` がノード辞書、`original["connections"]` が接続辞書。詳細スキーマは [../../../../references/tfl-json-schema.md](../../../../references/tfl-json-schema.md) 参照。

⚠️ `aux_for_new` を握り続けて Step 4 の `pack_flow_json` に渡すこと。これを忘れると `flow` だけの zip になり Server publish が拒否される。

### Step 3: ノード抽出と接続再構成

各新 .tfl について:

#### 3a. 該当ノードと接続を抽出

各ノードは [scripts/flow_io.py](../../../../scripts/flow_io.py) の `copy_source_node` 経由で取得する。素の `copy.deepcopy(original["nodes"][nid])` ではなく **必ず `copy_source_node` を使う**。理由: 後者は `nextNodes` の枝刈り (kept_children 外を削除) と `nextNamespace` の verbatim 保持を同時に行う。`nextNamespace` を手で書き換えると Union 入力の namespaceFieldMappings や Join の Left/Right 識別が壊れ、run 時に "Union step is missing a connection" / "missing field on left side" が発生する。

```python
from flow_io import copy_source_node

included_ids = {nid1, nid2, ...}  # 設計案から
included_nodes = {
    nid: copy_source_node(original, nid, kept_children=included_ids)
    for nid in included_ids
}
```

> ⚠️ 残ったエッジの `nextNamespace` を **絶対に手で上書きしない**。`Union-Namespace-<uuid>` / `Left` / `Right` を `Default` に潰すと silent failure を生む。

#### 3b. 切れた依存を新 Input ノードに置換 (LoadSqlProxy + 上流 PDS)

例: 新 `int_orders_enriched.tfl` の入力は stg レイヤの 2 つの PDS (`stg_snowflake__orders`, `stg_salesforce__opportunities`)。

元の依存（外部に出る前段）を **新規 `LoadSqlProxy` ノード** で置き換える (= 上流レイヤの Published Data Source を読む形)。`LoadHyper` (ローカル .hyper 参照) はローカル Prep Builder GUI でしか動かず Cloud 上で下流に繋がらないので、Cloud に publish する .tfl では使わない。

[scripts/flow_io.py](../../../../scripts/flow_io.py) の `add_pds_input` で Server 接続 / dataConnection 登録 / LoadSqlProxy ノード生成を一括実施。**子へのエッジ追加は `wire_new_input_to_child` で別途行う** — `add_pds_input(next_nodes=[...])` で生エッジを書くと `nextNamespace` を Default にハードコードしてしまう罠を避けるため:

```python
from flow_io import add_pds_input, wire_new_input_to_child

ctx = parse_deploy_context("deploy-context.md")  # server_url, site_url_name, layer LUIDs

lsp_id, _ = add_pds_input(
    new_flow,
    server_url=ctx["server_url"],
    site_url_name=ctx["site_url_name"],
    project_name=ctx["layer_projects"]["stg"]["ds_project"]["name"],  # 上流 PDS は datasources 配下 (例 "99_Sandbox/.../datasources/stg")
    datasource_name="stg_snowflake__orders",                          # 上流 flow 名 = PDS 名
    dbname=None,        # 上流 publish/run 完了後に prep-deployer が patch
    fields=upstream_fields_if_known,                                  # 不明なら []
    next_nodes=None,    # ← 必ず None。エッジは下の wire_* で張る
)

# この LSP が、元 .tfl ではどの parent ノードを置き換えているかを必ず指定する。
# replaced_source_parent_id = 「新 .tfl の child を、元 .tfl で feed していたノード」の ID。
# wire_new_input_to_child が source の (replaced_source_parent_id, child_id) エッジの
# nextNamespace (Union-Namespace-* / Left / Right) を新エッジに継承する。
wire_new_input_to_child(
    new_flow,
    lsp_node_id=lsp_id,
    child_id=dependent_node_id,
    source_flow=original,
    replaced_source_parent_id=src_parent_id_that_used_to_feed_this_child,
)
```

`add_pds_input` は (server_url, site_url_name) で Server 接続を dedup する。同 .tfl 内に複数の LoadSqlProxy が居ても Server 接続は 1 個に集約される (KB 005232681 の重複 connection エラー回避)。

> ⚠️ **LSP が Union / Join の入力を置き換えるとき** は `replaced_source_parent_id` の指定が必須。これがないと source の `Union-Namespace-<uuid>` / `Left` / `Right` が継承されず Default に潰れ、run 時に Union/Join 接続エラーになる。Step 4.5 の `verify_edge_namespaces` で fail-fast 検知できるが、入口で正しく張ること。

`dbname` の罠: Tableau Cloud に新規 publish された PDS は `<datasourceName>_<17桁ハッシュ>` 形式の物理 hyper 名を持ち、ビルド時には未確定。build 時は `dbname=None` で生成して構わない (run 前に deployer 側で解決される契約)。

具体的なノード組み立てパターンは [../../../../references/tfl-json-schema.md](../../../../references/tfl-json-schema.md#切れた依存を新-input-ノードに置換-推奨-loadsqlproxy--上流-pds) 参照。

#### 3c. actions 単位の分割実装

設計案の `## Actions-level splits` セクションに対象ノードがある場合、1 つの SuperTransform を 2 つ以上の新規 SuperTransform に分割する。

実装パターン・保全ルール（順序保持、空ノード削除、リワイヤ）は [../../../../references/tfl-json-schema.md](../../../../references/tfl-json-schema.md#supertransform-を-actions-単位で分割) 参照。

#### 3d. Output ノードの追加 (全レイヤ PublishExtract → datasources/<layer> project)

全レイヤで `PublishExtract` を採用し、PDS は `<target>/datasources/<layer>` に書く (flow .tfl 自体の publish 先 `<target>/flows/<layer>` とは別プロジェクト、[project-hierarchy.md](../../../../references/project-hierarchy.md))。`WriteToHyper` (ローカル書き出し) は Cloud で下流から参照できないため使わない。

[scripts/flow_io.py](../../../../scripts/flow_io.py) の `make_publish_extract_node` ヘルパ:

```python
from flow_io import make_publish_extract_node

layer = "stg"  # or "intermediate" / "marts"
out_node = make_publish_extract_node(
    project_name=ctx["layer_projects"][layer]["ds_project"]["name"],   # PDS publish 先 (例 "99_Sandbox/.../datasources/stg")
    project_luid=ctx["layer_projects"][layer]["ds_project"]["luid"],   # 同上の LUID (preflight が作成、deploy-context.md に記載)
    datasource_name=new_flow_name,                                     # flow 名 = PDS 名
    server_url=ctx["server_url"],
    site_url_name=ctx["site_url_name"],
)
new_flow["nodes"][out_node["id"]] = out_node
# 末端ノードの nextNodes に out_node の id を追加するのを忘れない
```

`projectLuid` は publish 後の取り違え事故を避けるため必須。`deploy-context.md` の preflight 完了後に datasources 配下の layer project LUID が確定するので、build はそれを読んで使う。

ctx の `layer_projects` 構造:

```python
ctx["layer_projects"][layer] = {
    "flow_project": {"name": "<target>/flows/<layer>",       "luid": "<flows-layer-luid>"},   # .tfl publish 先 (publish_flow.py が使う)
    "ds_project":   {"name": "<target>/datasources/<layer>", "luid": "<datasources-layer-luid>"}, # PDS publish 先 (LSP Input & PublishExtract が使う)
}
```

| 出力先 | nodeType（例） | 用途 |
|---|---|---|
| Published Data Source | `PublishExtract` | **全レイヤ標準** |
| ローカル `.hyper`（検証用のみ） | `WriteToHyper` | Prep Builder GUI 単体検証 |
| DB テーブル | `WriteToDatabase` | 既存 DWH への書き戻し |

#### 3d-2. mart の Rename-back ノード挿入 (Output mapping に行を持つ mart のみ)

plan の mart entry に `Rename-back (presentation rename)` 表がある場合 ([decomposition-plan-format.md §Rename-back](../../../../references/decomposition-plan-format.md))、**最終変換ノードと PublishExtract Output の間に専用 SuperTransform を 1 つ挿入**する。既存最終ノードへの actions 追記は列削除順序の罠を踏みうるため、独立ノードが安全。

```python
from flow_io import make_rename_supertransform

rb = make_rename_supertransform(
    renames=[("ticker", "銘柄"), ("settlement_date_買付", "約定日_買付")],  # plan の表の行順
    name="Rename-back",
)
new_flow["nodes"][rb["id"]] = rb
# wiring: <最終変換ノード> → rb → out_node (すべて Default edge)
add_edge(last_node, rb["id"])
add_edge(rb, out_node["id"])
```

verify: Rename-back 適用後の出力列集合が plan の Rename-back 表の `original name` を全て含み、`internal name` を 1 つも含まないこと (内部名の露出ゼロ)。

**⚠️ 出力 PDS シェルの凍結**: Prep が作る出力 PDS の .tds field メタデータは **PDS 初回作成時のスキーマで凍結され、以後の flow run では更新されない** (run は hyper 側のデータ/スキーマのみ更新)。新規 build なら初回 run 時点で rename-back 済みスキーマになるので問題ないが、**publish 済み PDS が存在する状態で後から出力スキーマを変える修正** (rename-back の後付け等) をすると、シェル (= Catalog / Metadata API が読む field list) と物理 hyper が乖離する。データサーバー経由の消費者 (Workbook / 下流 Prep) は hyper 側の正しいスキーマを見るが、Metadata API 検証は偽 FAIL になる。対処: PDS を削除して flow run で作り直す (LUID/content_url が変わる) か、full .tdsx を DL → .tds 内の旧名を書換 → Overwrite republish でシェルだけ直す (LUID/content_url 保持)。

#### 3d-3. incremental refresh / append 出力の継承 (元フローが incremental の場合のみ)

plan の該当 .tfl に「incremental 継承方針」がある場合 (decompose-self-check 項目 16、flow-summary.md の Meta `Incremental inputs` / `Append-mode outputs` が一次シグナル)、元フローの refresh 設定を新 .tfl に焼き込む。**append + incremental を引き継ぐ .tfl は通常「元 Output を引き継ぐ層」** (元フローが Output を 1 つ持つなら、その処理を含む層 = 多くは int または mart)。

```python
from flow_io import set_incremental_refresh

set_incremental_refresh(
    new_flow,
    input_node_id=<incremental input の LSP node id>,   # 元フローで incrementalEnabled=true だった入力に対応
    control_field="Date",                                # 元 IncrementalConfiguration.controlFieldName の caption
    output_node_id=out_node["id"],
    output_field="Date",                                 # 出力側 control 列 (通常 control_field と同名)
    is_incremental_default=True,                          # REST /run に runMode 引数が無いので default を incremental に
)
```

これは `flow["nodeProperties"]` に `IncrementalConfiguration` (入力) + `OutputRefreshOptions` (出力、append/append) を書き込む。build ヘルパは append/append のみ対応 (実証済みの組合せに限定)。

**運用上の重要な注意 (プラン Migration order + publish-recipe に反映)**:

- **append 出力は full run で重複する**。初回だけ full run で baseline を作り (新規 PDS = 現スナップショット 1 バッチ)、**以後は必ず incremental run** ([run_flow.py](../scripts/run_flow.py) `--incremental`)。full run を再度当てると 2 倍になる
- 元 PDS が過去の累積履歴を持つ場合、それは現ソースに残っていないので新 mart には初回 baseline 分しか入らない。**履歴 backfill が要るかは業務判断** (plan 項目 16)

#### 3e. 不要フィールドの除去

新 .tfl では:
- `nodes`: 抽出ノード ＋ 新規 Input/Output のみ残す
- `connections`: 該当する接続のみ残す
- `name`: 新 .tfl の名前に変更
- `loomVersion` 等のメタは保持

### Step 4: zip 化して .tfl 保存

```python
from flow_io import pack_flow_json
pack_flow_json(
    new_flow_json,
    "flows/intermediate/int_orders_enriched.tfl",
    aux_entries=aux_for_new,   # Step 2 で取得した maestroMetadata + displaySettings
)
```

`aux_entries=` を渡さないと publish 不能。Step 2 の冒頭で確保した `aux_for_new` を必ず渡す。

`.tflx` を作る場合は `.hyper` ファイルも zip 内に含める。MVP は `.tfl`（JSON のみ）で十分。

### Step 4.5: Lineage closure check + edge namespace check (両方必須)

各新 .tfl を `pack_flow_json` で書き出した直後、または全 .tfl 生成後に一括で 2 種のチェックを通す:

```python
from flow_io import (
    load_flow_json,
    verify_lineage_closure,
    verify_edge_namespaces,
)

source = load_flow_json(src_tfl_path)

# (1) Lineage closure: 含めたステップが新 Input から到達可能か
synthetic_input_lineage = {
    "stg_orders":         [SRC_INPUT_SNOWFLAKE_ID],
    "stg_opportunities":  [SRC_INPUT_SALESFORCE_ID],
    "int_orders_enriched": [SRC_INPUT_SNOWFLAKE_ID, SRC_INPUT_SALESFORCE_ID],
    # ...
}

# (2) Edge namespace: parent->Union/Join の nextNamespace が Default に潰れていないか。
# LSP が source parent を置き換えるペアは parent_substitutions で宣言する。
# Key = 新 .tfl 内 LSP の node id, Value = source 側の元 parent id
parent_substitutions_per_tfl: dict[str, dict[str, str]] = {
    # 例: "int_orders_enriched": {"<lsp-id>": SRC_PARENT_FED_THE_REPLACED_CHILD}
}

for path in generated_tfls:
    new = load_flow_json(path)

    issues_a = verify_lineage_closure(new, source,
                                      synthetic_input_lineage=synthetic_input_lineage)
    issues_b = verify_edge_namespaces(new, source,
                                      parent_substitutions=parent_substitutions_per_tfl.get(path.stem, {}))
    issues = issues_a + issues_b
    if issues:
        raise RuntimeError(f"{path}: build verification failed:\n  - " + "\n  - ".join(issues))
```

検知する 2 種:
- **Lineage break**: ステップが宣言 Input から到達不能 = decompose の取り違え
- **Namespace mismatch**: parent→Union/Join のエッジで `nextNamespace` が Default に潰れている = build の namespace 喪失バグの silent failure を build 段階で fail-fast

何を検知するか: **「new .tfl の宣言 Inputs のどれを辿っても、含まれているステップに source DAG 上で到達しない」** ケース。例: Clean 4 (`Symbol = TRIM(REPLACE(SPLIT([ID])))`) を Transactions branch の stg flow に誤配置 → source DAG では Clean 4 は input_market (PDS) 側の子孫で Transactions Input からは到達できない → `lineage break: node 0ffb5436 ('Clean 4')` が出る。

検知されたら build を中断して decompose に差し戻す (該当ステップを正しいレイヤ / .tfl に再配置)。自動修復はしない (誤配置の意図解釈は危険)。

詳細な原理は decompose 側 [../../../../references/decomposition-plan-format.md](../../../../references/decomposition-plan-format.md) の Lineage closure invariant 節を参照。

### Step 4.6: publish-manifest 初期化 / 保持

全 .tfl を書き出して検証を通したら、session manifest を扱う。**既存の `publish-manifest.json` が `--output` 位置にあれば、原則保持** (上書きしない)。`init` は新規セッション (manifest 不在) のときだけ呼ぶ。

```bash
python scripts/publish_manifest.py init \
  --decomposition-plan <session>/reports/decomposition-plan-<flow>.md \
  --flow-summary <session>/reports/flow-summary.md \
  --flows-dir <session>/flows \
  --output <session>/reports/publish-manifest.json \
  --original-flow-luid <luid-if-known>
```

挙動:

- `--output` 既存ファイルなしの場合: `publish-manifest.json` を `status="pending"` / `luid=null` で新規生成
- `--output` 既存ファイルありの場合: **デフォルトで exit 1 + エラーメッセージ** (publish/run 状態を失わないため)。再 build (例: decompose 修正後の rebuild) であってもこの保護は有効

スクリプトの動作 (新規生成時):

- decomposition-plan の `## Output mapping (original → decomposed)` 表をパースして元 PDS と marts 新 flow の対応を取得
- flow-summary.md から元フロー名と元 output PDS リストを抽出
- `<session>/flows/{staging,intermediate,marts}/*.tfl` をスキャンして各 .tfl の PublishExtract output PDS を抽出
- これらをマージして `publish-manifest.json` を書き出す

`--original-flow-luid` は session intake Q1 で確定していれば渡す。null でも `resolve-luids` フェーズ (prep-deployer 後段) で名前から逆引きされるので任意。

#### 既存 manifest を意図的に上書きしたいとき

decomposition-plan の Output mapping を変更した、`flows/` から .tfl を増減した等、**manifest の構造自体を再生成したい** 場合のみ `--force` を付ける:

```bash
python scripts/publish_manifest.py init ... --force
```

publish/run 状態は **完全に失われる** ので、上書き前に手動で `cp publish-manifest.json publish-manifest.json.bak` を取り、必要なら後で merge する。再 build したが Output mapping は変えていない場合は `--force` を付けないこと (既存 manifest がそのまま正しい)。

manifest format は [../../../../references/publish-manifest-format.md](../../../../references/publish-manifest-format.md)。

### Step 5: サマリ出力

```markdown
## Build summary

Generated 7 new .tfl files:
  - flows/staging/stg_salesforce__opportunities.tfl
  - flows/staging/stg_snowflake__orders.tfl
  - flows/staging/stg_snowflake__customers.tfl
  - flows/intermediate/int_orders_enriched.tfl
  - flows/intermediate/int_customer_classified.tfl
  - flows/marts/fct_sales.tfl
  - flows/marts/dim_customer.tfl

Actions-level splits applied (2 ノード):
  - 元 Clean 1: Rename×4 を stg__transactions に、ROW_NUMBER LOD を int_orders_enriched に振り分け
  - 元 Clean 2: ChangeType を stg に、FIXED MAX/Filter を int に振り分け

Next steps for the user:
  1. Tableau Prep Builder で各 .tfl を開いて単体動作を確認
  2. 動作確認後、prep-deployer の publish へ進む (session intake で goal=④ なら自動継続)
```

## エラーハンドリング

| エラー | 挙動 |
|---|---|
| 設計案 markdown のパースに失敗 | 中断、形式エラーを報告 |
| 元 .tfl の zip 展開に失敗 | 中断、ファイル破損の可能性 |
| 未知の nodeType を含むノードを抽出 | そのまま転写（中身は変えない）＋ ユーザー警告 |
| 抽出ノード群に循環依存 | 中断、設計案の DAG を再確認するよう報告 |
| `flows/<layer>/<name>.tfl` が既存 | **上書きしない**、警告してユーザーに確認 |
| Output ノードを追加できない（型情報不足） | その .tfl をスキップ、レポートに「skipped: missing schema info」 |
| zip 書き込みに失敗 | 中断、ディスク容量・権限を確認するよう報告 |

## 検証

build 完了後、ユーザーに **必ず手動検証** を案内:

1. Tableau Prep Builder で各 .tfl を開く
2. Input ノードがエラーにならないか確認
3. プレビュー実行 → 想定通りの出力スキーマか確認
4. 元フローと比較して数値一致を確認

検証が終わったら prep-deployer の publish へ進む (session intake の goal=④ で publish も合意済みの場合は自動で続行、ローカル動作確認だけで止めたい場合は session intake で goal=③ を選ぶ)。

## 制約

MVP では:

- 自動マイグレーション（Tableau Cloud 上での仮想接続作成等）は **しない**
- DB View の自動生成・自動デプロイは **しない**
- Calculated Field の自動定義は **しない** — Tableau Desktop での手動設定

MVP の責務は **設計案を実体ある .tfl ファイル群に落とす** ところまで。

## 元 .tfl の Input が仮想接続 (VConn) の場合 — PDS 化を案内

源 flow の Input が **Tableau Cloud Virtual Connection (仮想接続)** (= `connections[].connectionAttributes.class == "publishedConnection"`) のときは、prep-builder で stg .tfl を組んでもそのまま Cloud で動かないケースがある:

- VConn は背後の DB / Google Sheets / etc. を抽象化するため、`fields` の `name` は UUID で `caption` が実カラム名 (多言語含む)
- Cloud 上で flow を run すると VConn の現在のスキーマと .tfl 内 `fields` 配列が一致しないことがある (列追加/削除/型変更が VConn 側で起きていた場合)
- 後続ステップの calc が **存在しない列名** を参照するケースもあり、これは元 flow の編集時点のメタデータと現在の VConn スキーマの drift

**推奨対応**: prep-builder 側で VConn Input を検知したら、ユーザーに対して次のいずれかを案内する:

1. **(推奨) その VConn の出力を Tableau Cloud 上で一度 PDS 化してもらう** (Prep Builder GUI で VConn → PDS を吐き出す flow を 1 つ動かす)、その PDS を新しい stg flow の Input にする。以降 prep-builder は uniform に LoadSqlProxy + PDS で組める
2. (非推奨) VConn のまま使う — その場合は `connections` / `dataConnections` を元 .tfl からそのまま継承し、対象 site で同じ VConn が利用可能であることを事前確認。スキーマ drift が起きていれば手動修正が必要

VConn 入力 stg flow の自動ハンドリングはスコープ外。複雑性 (VConn の認証/権限/スキーマ管理) はユーザー判断に委ねる。

## 設計上の前提

- **cross-layer Input は LoadSqlProxy 一択** (Step 3b)。`add_pds_input` が Server 接続 / dataConnection / node を一括登録 + Server 接続を (server_url, site_url_name) で dedup。`project_name` は上流レイヤの `ds_project` (= `<target>/datasources/<upstream-layer>`)
- **全レイヤ Output は PublishExtract → `<target>/datasources/<layer>`** (Step 3d)。`projectLuid` は preflight 後の deploy-context.md から取得 (`ds_project.luid`)
- **flow .tfl 本体の publish 先は `<target>/flows/<layer>`** で、PDS publish 先 (`<target>/datasources/<layer>`) とは別プロジェクト。publish_flow.py 呼び出し時の `--project-path` は `flow_project.name` を使う
- **`dbname` は build 時 None で OK** (run 前に deployer 側で解決される契約)

## 検証順序 (レイヤ依存を踏まえた build/validate)

B2 修正後は、検証 (= publish 試行) に **レイヤ順依存** が発生する。int/marts は上流レイヤの PDS が Cloud 上に存在しないと publish 段階で `Input data source not found` で弾かれる可能性が高い。

```
1. build all (stg + int + marts 全部 .tfl 生成、provisional でも OK)
        ↓
2. stg: publish → run → PDS 作成完了を待つ
        ↓
3. int: publish (上流 stg の PDS が存在するので通る) → run → PDS 作成完了
        ↓
4. marts: publish → run → PDS 作成完了
```

並列化できるのは **同一レイヤ内の複数 .tfl** だけ。レイヤ間は必ず順次 (依存関係上のゲートであって承認ゲートではない)。失敗時は prep-deployer 側で [autonomous-recovery](../../prep-deployer/references/autonomous-recovery.md) に従って分類 → 自律対処、escalation 発火時のみ次レイヤに進まない。

## 実装方針

組み立てロジックは Skill 内の LLM が [scripts/flow_io.py](../../../../scripts/flow_io.py) を直接叩いて行う。決定論的な CLI ラッパー (`emit_tfl.py` 等) のスクリプト化は、実フローでの build を複数回回して変換パターンが安定してから検討する。

## 参考

- .tfl JSON スキーマ・組み立てパターン: [../../../../references/tfl-json-schema.md](../../../../references/tfl-json-schema.md)
- 命名規約: [../../../../references/naming-conventions.md](../../../../references/naming-conventions.md)
- レイヤ責務: [../../../../references/layer-responsibilities.md](../../../../references/layer-responsibilities.md)
- decompose の出力書式: [../../../../references/decomposition-plan-format.md](../../../../references/decomposition-plan-format.md)
