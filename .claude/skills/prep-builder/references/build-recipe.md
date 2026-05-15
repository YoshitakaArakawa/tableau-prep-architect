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
3. 各新 .tfl ごとに:
     a. 設計案から「含めるべき元ステップ ID」を取得
     b. 元 JSON から該当ノード＋接続定義を抽出
     c. 切れた依存（前段が別 .tfl になる）は新 Input ノードに置き換え
     d. 設計案に actions 単位の分割指示があれば SuperTransform の beforeActionAnnotations を振り分け
     e. 末端ノードに新しい Output ノードを追加
     f. 新 flow JSON を組み立て
     g. zip 化して .tfl として保存
   ↓
4. 生成サマリをユーザーに報告
```

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
    project_name=ctx["layer_projects"]["stg"]["name"],          # 例 "99_Sandbox/.../stg"
    datasource_name="stg_snowflake__orders",                    # 上流 flow 名 = PDS 名
    dbname=None,        # 上流 publish/run 完了後に prep-deployer が patch
    fields=upstream_fields_if_known,                            # 不明なら []
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

#### 3d. Output ノードの追加 (全レイヤ PublishExtract → layer project)

全レイヤで `PublishExtract` を採用し、その flow が置かれる layer project と同じ場所に PDS を書く (stg flow → `<target>/stg` / int → `<target>/intermediate` / marts → `<target>/marts`)。`WriteToHyper` (ローカル書き出し) は Cloud で下流から参照できないため使わない。

[scripts/flow_io.py](../../../../scripts/flow_io.py) の `make_publish_extract_node` ヘルパ:

```python
from flow_io import make_publish_extract_node

layer = "stg"  # or "intermediate" / "marts"
out_node = make_publish_extract_node(
    project_name=ctx["layer_projects"][layer]["name"],   # 例 "99_Sandbox/.../stg"
    project_luid=ctx["layer_projects"][layer]["luid"],   # preflight が作成、deploy-context.md に記載
    datasource_name=new_flow_name,                       # flow 名 = PDS 名
    server_url=ctx["server_url"],
    site_url_name=ctx["site_url_name"],
)
new_flow["nodes"][out_node["id"]] = out_node
# 末端ノードの nextNodes に out_node の id を追加するのを忘れない
```

`projectLuid` は publish 後の取り違え事故を避けるため必須。`deploy-context.md` の preflight 完了後に layer project の LUID が確定するので、build はそれを読んで使う。

| 出力先 | nodeType（例） | 用途 |
|---|---|---|
| Published Data Source | `PublishExtract` | **全レイヤ標準** |
| ローカル `.hyper`（検証用のみ） | `WriteToHyper` | Prep Builder GUI 単体検証 |
| DB テーブル | `WriteToDatabase` | 既存 DWH への書き戻し |

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

- **cross-layer Input は LoadSqlProxy 一択** (Step 3b)。`add_pds_input` が Server 接続 / dataConnection / node を一括登録 + Server 接続を (server_url, site_url_name) で dedup
- **全レイヤ Output は PublishExtract → 同レイヤ project** (Step 3d)。`projectLuid` は preflight 後の deploy-context.md から取得
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
