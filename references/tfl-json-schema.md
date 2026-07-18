---
purpose: .tfl / .tflx ファイル構造と flow.json のスキーマリファレンス。UI ステップ ⇔ nodeType の対応表も含む
sources:
  - https://help.tableau.com/current/prep/en-us/prep_save_share.htm
  - https://help.tableau.com/current/prep/en-us/
fetched_at: 2026-05-17
source_last_known_update: 不明（公式は包括的スキーマ docs を公開していないため実例ベースで埋めている）
note: ファイル形式 (zip + flow + .hyper)、トップレベル JSON 構造、UI ステップ ⇔ nodeType / actions サブタイプ対応表、依存関係の罠、新規 .tfl 組み立てパターン、SuperTransform を actions 単位で分割する実装手順を含む
---

# tfl-json-schema

`.tfl` / `.tflx` ファイルの構造リファレンス。**tableau-prep-extractor** が flow.json を読むとき、**tableau-prep-builder** が新規 .tfl を組み立てるときに参照する。

**目次**:

1. ファイル形式 — zip 内 entry 詳細 / maestroMetadata 規約
2. トップレベル JSON 構造（典型） — Input 接続グラフの 2 形式 (wrapped / direct)
3. UI ステップ ⇔ nodeType マッピング — バージョンプレフィクス
4. ノードの典型構造
5. ⚠️ 依存関係の表現（実地で見つけた重要事項）
6. Clean ステップの 2 つのシリアライズ形式 — Container 形式の正規化
7. SuperTransform 内部の actions — actions サブタイプ ⇔ UI 操作
8. 新規 .tfl 組み立てパターン（tableau-prep-builder build 用） — Input 置換 / Output 種別 / actions 分割 / zip 化
9. 接続定義 (DB 直接接続の例)
10. .tfl と .tflx の使い分け / バージョン互換 / 未知のノード種別への対処

⚠️ 一般的な情報ベース。Tableau Prep バージョン間で構造が変わる可能性があるため、**実 .tfl サンプルで検証してから利用すること**。公式の包括的スキーマドキュメントは存在しない。

## ファイル形式

| 拡張子 | 中身 |
|---|---|
| `.tfl` | **zip アーカイブ**。複数 entry を含む (詳細は次節) |
| `.tflx` | **zip アーカイブ**。`.tfl` の内容 ＋ 抽出データ (`.hyper`) |

.tfl は zip アーカイブで、`flow` エントリが JSON 本体。展開・パックは repo 直下 [scripts/flow_io.py](../scripts/flow_io.py) の `unpack_flow_json` / `pack_flow_json` を使う。

### `.tfl` の zip 内 entry (詳細)

Prep Builder が保存する .tfl は **マルチエントリ zip**。最低限以下を含む:

| entry | 必須か | 役割 |
|---|---|---|
| `flow` | ✅ 常に必須 | フロー定義 JSON |
| `maestroMetadata` | ✅ **publish に必須** | Tableau Prep (社内コードネーム "Maestro") のメタデータ。これが無いと Server publish が publish 拒否シグネチャ **280003** (`errorCode=280003 "Problem reading the provided Flow file"`、詳細分類は [tableau-prep-deployer の autonomous-recovery.md](../.claude/skills/tableau-prep-deployer/references/autonomous-recovery.md)。本 doc では以降 "(→ 280003)" と表記) で拒否、Prep CLI ロードも `InvalidMaestroDocumentMetadataNotFoundMsg` で失敗 |
| `displaySettings` | 推奨 | UI レイアウト・pane 状態。無くても publish は通るが Prep Builder で開くと初期表示が崩れる |
| `flowGraphImage.png` | 不要 | Prep Builder UI 上のプレビュー画像 (生成元 DAG と整合しない場合は同梱しない方が安全) |
| `flowGraphThumbnail.svg` | 不要 | 同上のサムネ |

**ビルダー実装上の規約**: 新規 .tfl を組み立てる場合、`maestroMetadata` を **元 .tfl からそのままコピーして** 同梱すること。空 .tfl や `flow` だけの .tfl は publish 不能。`scripts/flow_io.py` の `pack_flow_json(..., aux_entries={...})` と `load_aux_entries(...)` および定数 `PUBLISHABLE_AUX_ENTRIES` がこの規約を支える。

⚠️ `maestroMetadata` は元フロー全体のメタなので、新 .tfl の DAG が元の部分集合のとき **内部に存在しないノード ID への参照を含む可能性がある**。publish は通っても、Prep Builder GUI で開いた時 / Cloud で run した時に不整合が露見するリスクは残る (現時点で未検証、要 Phase 2)。

## トップレベル JSON 構造（典型）

```json
{
  "name": "My Flow",
  "version": "...",
  "loomVersion": "...",
  "nodes": { "<node-uuid>": { ... }, ... },
  "connections": { "<connection-id>": { ... }, ... },
  "connectionsAttributes": [...],
  "initialNodes": [ "<entry-node-uuid>", ... ]
}
```

| キー | 役割 |
|---|---|
| `nodes` | 各ステップ（Input / Clean / Join / Aggregate / Output 等）の定義辞書 |
| `connections` | データソース接続定義（DB ホスト、テーブル名等） |
| `connectionsAttributes` | 接続の付帯情報 |
| `initialNodes` | エントリーポイント（依存のない先頭ノード）の UUID 配列 |
| `name`, `version`, `loomVersion` | メタ情報 |

### Input の接続グラフ：2 つのシリアライズ形式

`LoadSql` Input の `connectionId` から実接続に辿る経路が 2 通りある（実サンプルでは Clean ステップの形式と 1:1 で共変する）:

| 形式（本 doc 内の呼称） | 経路 | `dataConnections` |
|---|---|---|
| **wrapped** | `connectionId` → `dataConnections[id]` → `.baseConnectionId` → `connections[base]` | 使用（wrapper 層） |
| **direct** | `connectionId` → `connections[id]` へ**直接** | 空 |

どちらも実接続オブジェクトの `connectionAttributes` に `class`（`publishedConnection` = vconn / `sqlproxy` / DB driver）と vconn 識別子（`resourceId` = vconn LUID、`resourceName` = caption）を持つ。`flow_io.inspect_input_node` / `build_helpers.transplant_source_input` は両形式を吸収する（`connectionId` が `dataConnections` に有れば wrapped、無く `connections` に直接有れば direct）。新規に Input を組む `add_pds_input` は常に wrapped で書く。

## UI ステップ ⇔ nodeType マッピング

⚠️ `Filter` / `Rename` / `AddColumn` などは **トップレベル nodeType ではなく、SuperTransform 内部の actions サブタイプ**。actions の対応表は本ファイル下方「[SuperTransform 内部の actions](#supertransform-内部の-actions)」節を参照。

トップレベル `nodeType`（末尾だけ示す）:

| UI ステップ | 内部 nodeType | 備考 |
|---|---|---|
| Input - SQL / Custom SQL | `LoadSql` | 通常の SQL ベース Input。connection が `sqlproxy` なら仮想接続経由の可能性大 |
| Input - Published Data Source | `LoadSqlProxy` | Tableau Server プロキシ経由 |
| Input - CSV | `LoadCsv` | ファイル入力 |
| Input - Excel | `LoadExcel` | ファイル入力 |
| Input - Hyper | `LoadHyper` | 中間 .hyper ファイル入力 |
| Clean ステップ | `SuperTransform` または `Container` | 複数 actions を内包する万能ステップ。`.v2018_2_3.SuperTransform` と `.v1.Container` の 2 通りで表現される（下記「Clean ステップの 2 つのシリアライズ形式」参照） |
| Join ステップ | `SuperJoin` | 結合 |
| Union ステップ | `SuperUnion` | UNION |
| Aggregate ステップ | `SuperAggregate` | 集約 |
| Pivot ステップ | `SuperPivot`（バージョン依存） | 縦横ピボット |
| New Rows ステップ | `SuperNewRows` | 時系列補間、連番生成、null 行追加 |
| Python / R ステップ | `Script` 系（要サンプル確認） | 外部スクリプト |
| Output - Hyper（ローカル/ファイル） | `WriteToHyper` | .hyper 書き出し |
| Output - Published Data Source | `PublishExtract` | Tableau Server へ publish |
| Output - Database | `WriteToDatabase` | DB テーブル書き出し |

### バージョンプレフィクス

`nodeType` は `.v<year>_<minor>_<patch>.<Type>` 形式。

例:
- `.v2018_2_3.SuperTransform`
- `.v2019_3_1.LoadSqlProxy`
- `.v2021_3_1.SuperNewRows`

同じ論理ステップでもバージョン違いの internal type が同一フロー内に混在することがある（フロー作成・編集された Tableau Prep のバージョンで決まる）。グルーピング時は **最後のドット以降** を使う。

業務的解釈は本ファイルの範囲外:
- レイヤ示唆: [layer-responsibilities.md](layer-responsibilities.md)

## ノードの典型構造

```json
"<node-uuid>": {
  "name": "<display name>",
  "nodeType": "...v2018_2_3.SuperTransform",
  "baseType": "...",
  "nextNodes": [
    {"nextNodeId": "<next-node-uuid>", ...}
  ],
  "previousNodes": [],
  "beforeActionAnnotations": [
    {"annotationNode": { "nodeType": "...RenameColumn", "columnName": "...", "rename": "..." }},
    {"annotationNode": { "nodeType": "...AddColumn", "columnName": "...", "expression": "..." }}
  ]
}
```

## ⚠️ 依存関係の表現（実地で見つけた重要事項）

- **`previousNodes` は空配列が普通** — トポロジ復元には使えない。`nextNodes` から **逆引きで前段を求める**
- **`nextNodes` の要素は dict**（文字列ではない）。`{"nextNodeId": "<uuid>"}` から取り出す
- **トポロジカル順序** は `flow["initialNodes"]` を起点に `nextNodes` を辿る **BFS** で復元する。visited 順が topological order となり、短 ID (#1, #2, ...) はこの順に採番する

## Clean ステップの 2 つのシリアライズ形式

同じ「Clean ステップ内の操作列」が、フローによって 2 通りにシリアライズされる。**論理的には等価**（`container_to_supertransform` で相互変換できる）。実サンプルでは 1 フロー内はどちらか一方に統一されている。

| 形式（本 doc 内の呼称） | トップレベル nodeType | 操作の格納場所 | 各操作の形 |
|---|---|---|---|
| **flat 形式** | `.v2018_2_3.SuperTransform` | `beforeActionAnnotations[]` | `{"namespace": ..., "annotationNode": {<操作>}}` |
| **Container 形式** | `.v1.Container` | `loomContainer.nodes{}`（子ノードが `nextNodes` で線形チェーン） | 子ノードそのものが操作（`.v1.AddColumn` 等） |

両形式とも、**操作 1 個の実体は同一の dict**（`nodeType` / `columnName` / `rename` / `expression` / `columnNames` / `filterExpression` 等を直接持つ）。違いは「annotationNode でラップされているか」と「チェーンが配列か nextNodes か」だけ。

Input ノード（`LoadSql` / `LoadSqlProxy`）は上記いずれとも別で、`node["actions"]` 配列に操作を持つ。難読化フロー（`obfuscatorId` 有り）では列名が UUID にハッシュ化され、**表示名（日本語含む）は Input の `actions` の RenameColumn（UUID → caption）と各 field の `caption` に現れる**。列名の意味を読むにはこの層を見る。

### Container 形式の正規化

`scripts/flow_io.py` が Container 形式 ↔ flat 形式のブリッジを提供する:

- `iter_container_children(node)` — Container の子操作をチェーン順に返す（annotationNode と同形の dict）
- `container_convertibility(node)` — 損失なく flat 形式へ変換可能か判定。`[]` なら可、非空なら阻害理由（マルチ namespace / 分岐 / ネスト Container）。**空の Clean ステップ**（子ノードゼロ）は変換可（= actions=0 の SuperTransform 相当）
- `container_to_supertransform(node)` — 単一 Container を flat SuperTransform に変換（id / name / nextNodes を保持）
- `normalize_source_containers(source_flow)` — フロー全体を 1 パスで正規化し `(normalized_flow, skipped_names)` を返す。**build の冒頭で 1 度呼ぶ**と以後の全処理（`copy_source_node` / `split_supertransform_actions` / verify / actions 分割）が Container を意識せず flat 形式として扱える。node id を保つので build スクリプトの node-id 定数はそのまま解決する

変換後の flat 形式の action dict は、Cloud で run 実績のある flat 形式フローの annotationNode と構造同一（同一キー集合）。非変換 Container は verbatim 転写のみ可（layer またぎの actions 分割は不可）。

## SuperTransform 内部の actions

- SuperTransform ノードの UI 上の操作（Rename / AddColumn / RemoveColumns / Filter / ChangeColumnType / ValueFilter 等）は **すべて `beforeActionAnnotations` 配列** に格納（Container 形式は前節参照）
- 各要素は `{"annotationNode": {...}}` でラップ。**1 階下ろしてから** `nodeType` と各種フィールド（`columnName`, `rename`, `expression`, `columnNames` 等）にアクセス
- `node.get("actions")` という見るからにそれっぽいフィールドは、SuperTransform では **空または存在しない**（罠）。ただし **Input ノードでは `actions` に RenameColumn 等が入る**（前節）
- action の `nodeType` も version prefix 付き。末尾だけ見れば下表の type 名になる

### actions サブタイプ ⇔ UI 操作

1 つの SuperTransform に **複数の actions** が並ぶ。各 action が UI 上の個別操作に対応:

| action type（末尾） | UI 操作 |
|---|---|
| `RenameColumn` | 列リネーム |
| `ChangeColumnType` | 型キャスト |
| `RemoveColumns` | 列削除 |
| `ValueFilter` | 値フィルタ（IS NOT NULL, = 'x' 等） |
| `FilterOperation` | 条件フィルタ |
| `AddColumn` | 計算列追加（IF/CASE/CONCAT/LOD 等） |
| `GroupValues` | 値のグループ化（"USA"/"U.S.A." → "US"） |
| `Split` | 列分割（氏名 → 姓・名） |
| `TrimWhitespace` | 前後空白除去 |
| `FixCase` | 大文字小文字統一 |
| `ReplaceValue` | 値置換 |

⚠️ action type の正確な命名は要検証（実例ベースの推定）。本表に無い action type / nodeType に遭遇したら追記する。実例で未観測のため要検証: Pivot ステップの正確な internal type / Custom SQL Input の細部 / 仮想接続経由 Input の typeRef / Python / R ステップの actual type / Filter / AddColumn の actions 構造の詳細フィールド。

## 新規 .tfl 組み立てパターン（tableau-prep-builder build 用）

build フェーズが新規 .tfl を生成する際の頻出パターン:

### 切れた依存を新 Input ノードに置換 (推奨: LoadSqlProxy + 上流 PDS)

元 .tfl から一部ノードを切り出すと、外部依存になった前段を新規 Input ノードで置き換える。Tableau Cloud 上で stg → int → marts のレイヤ間を繋ぐには、**LoadSqlProxy で上流レイヤの Published Data Source を参照** する形にする ([tableau-prep-builder の build-recipe.md](../.claude/skills/tableau-prep-builder/references/build-recipe.md) B2 修正)。`LoadHyper` (ローカル `.hyper` 参照) は Tableau Cloud 上では下流から参照できないので不適。

`LoadSqlProxy` ノードを 1 個入れる際には、必ず以下の 4 つも揃える (どれかが欠けると publish 拒否 (→ 280003)):

1. **トップレベル `connections[<conn-id>]`** — Tableau Server 接続 (class=sqlproxy)
2. **トップレベル `dataConnections[<dconn-id>]`** — その PDS への接続 (baseConnectionId が ↑ の conn-id を指す)
3. **トップレベル `connectionIds` / `dataConnectionIds`** 配列に上記 id を追加
4. **LoadSqlProxy ノードの `connectionId`** が **dataConnection の id** を指す (connection の id ではない)

実装は [scripts/flow_io.py](../scripts/flow_io.py) の `register_server_connection` / `register_pds_data_connection` / `make_load_sql_proxy_node` ヘルパが面倒を見る。複数の LoadSqlProxy が同じ Server 接続を共有するよう、`register_server_connection` は (server_url, site_url_name) で dedup する (重複 Tableau Server 接続も publish 拒否を引く (→ 280003, KB 005232681))。

LoadSqlProxy ノードの典型構造:

```json
{
  "nodeType": ".v2019_3_1.LoadSqlProxy",
  "id": "<uuid>",
  "name": "<datasourceName> (<projectName>)",
  "baseType": "input",
  "nextNodes": [{"namespace": "Default", "nextNodeId": "<next>", "nextNamespace": "Default"}],
  "connectionId": "<dataConnection-uuid>",
  "connectionAttributes": {
    "dbname": "<physical-hyper-name>",
    "projectName": "<cloud-project>",
    "datasourceName": "<pds-name>"
  },
  "fields": [ ... ]
}
```

Server 接続エントリ (`connections[<conn-id>]`):

```json
{
  "connectionType": ".v1.SqlConnection",
  "id": "<conn-uuid>",
  "name": "<server> (<site>)",
  "isPackaged": false,
  "connectionAttributes": {
    "server": "https://<host>",
    "port": "443",
    "query-category": "Data",
    "siteUrlName": "<site-url-name>",
    "channel": "https",
    "class": "sqlproxy",
    "directory": "/dataserver",
    "odbc-native-protocol": "yes"
  }
}
```

PDS dataConnection エントリ (`dataConnections[<dconn-id>]`):

```json
{
  "connectionType": ".QueryDataConnection",
  "id": "<dconn-uuid>",
  "name": "<server> (<site>)",
  "isPackaged": false,
  "baseConnectionId": "<conn-uuid>",
  "modifiedConnectionAttributes": {
    "dbname": "<physical-hyper-name>",
    "projectName": "<cloud-project>",
    "datasourceName": "<pds-name>"
  }
}
```

⚠️ `dbname` の罠:

- **publish 時には `dbname` が必須**。LoadSqlProxy の `connectionAttributes.dbname` と dataConnection の `modifiedConnectionAttributes.dbname` のどちらかでも欠けると publish 拒否 (→ 280003)
- ただし publish-time validation は値の妥当性をチェックしない (= 任意の placeholder 文字列で publish 通る)
- run 時には実 dbname が必要 (= 不一致だと `Input data source not found` 系で finishCode=1)
- Tableau Cloud は新規 publish された PDS に `<datasourceName>_<17桁ハッシュ>` 形式の物理 hyper 名を割り振る (ビルド時点では未確定)
- ヘルパ (`flow_io.add_pds_input`) は `dbname=None` 渡しても `<datasourceName>_placeholder` を自動挿入するので publish は通る。run 前に tableau-prep-deployer の `discover_pds_dbname.py` で上流 PDS の実 dbname を解決して patch する
- 既存フローの `dbname` は **旧名の残骸のことがある** (PDS が rename されても dbname は追従せず、実在しない名前を保持し続ける)。フローの実参照先を判定するときは `datasourceName` + `projectName` を正とし、dbname から逆推定しない

### LoadHyper (ローカル `.hyper` 参照、Cloud では使えない)

Prep Builder GUI 単体検証用 (Cloud に上げない短期検証) なら LoadHyper でも良い。ノードのキーは `nodeType` (`"LoadHyper"`) / `name` / `filePath` (ローカル `.hyper` への相対パス) / `nextNodes` / `previousNodes`。

ただし Cloud に publish しても下流 run でそのローカル `.hyper` は参照できない。`connectionId=None` + connections 空のままだと publish 自体が弾かれる (→ 280003。現状の tableau-prep-builder の制限 B1)。Cloud に上げる .tfl では **必ず LoadSqlProxy** を使う。

### Output ノードの種別ガイド

| 出力先 | nodeType（例） | 推奨用途 |
|---|---|---|
| Published Data Source | `PublishExtract` | **全レイヤ標準** (stg / int / marts どれでも Cloud 上で下流に繋げるため) |
| 中間 Hyper（ローカル） | `WriteToHyper` | Prep Builder GUI 単体検証用のみ。Cloud では下流から参照不能 |
| DB テーブル | `WriteToDatabase` | 既存 DWH に書き戻す例外ケース |

PublishExtract ノードの典型構造:

```json
{
  "nodeType": ".v1.PublishExtract",
  "id": "<uuid>",
  "name": "Output",
  "baseType": "output",
  "nextNodes": [],
  "projectName": "<cloud-project>",
  "projectLuid": "<project-luid>",
  "datasourceName": "<pds-name>",
  "datasourceDescription": "",
  "serverUrl": "https://<host>/#/site/<site-url-name>"
}
```

各レイヤの flow の PublishExtract Output は、その flow の layer に対応する **`datasources/<layer>` プロジェクト** に書く (stg → `<target>/datasources/stg` / int → `<target>/datasources/intermediate` / marts → `<target>/datasources/marts`。[project-hierarchy.md](project-hierarchy.md))。`projectLuid` は build 時に **plan.json の `ds_projects.<layer>.luid` から取る** (`gen_plan_skeleton` が preflight → Phase B 再実行後の `deploy-context.md` から充填。[plan-json-schema.md](plan-json-schema.md))。`datasourceName` は flow 名と一致 (例 `stg_transactions` → `stg_transactions` PDS)。

### SuperTransform を actions 単位で分割

⚠️ 元フローが Container 形式（`.v1.Container` の Clean ステップを含む）の場合、**build の冒頭で `normalize_source_containers(source_flow)` を 1 度呼んでから** 以下を行う（前節「Container 形式の正規化」）。正規化後は Container が flat SuperTransform になっているので、下記パターンがそのまま適用できる。

1 つの SuperTransform を 2 つ以上に分け、別 .tfl に振り分けるパターン:

```python
import copy
src_node = original["nodes"]["<old-supertransform-id>"]
all_actions = src_node["beforeActionAnnotations"]

stg_node = copy.deepcopy(src_node)
stg_node["id"] = "<new-id-1>"
stg_node["name"] = "Clean 1 (Rename only)"
stg_node["beforeActionAnnotations"] = [all_actions[i] for i in [0, 1, 2, 3]]

int_node = copy.deepcopy(src_node)
int_node["id"] = "<new-id-2>"
int_node["name"] = "Clean 1 (ROW_NUMBER LOD)"
int_node["beforeActionAnnotations"] = [all_actions[i] for i in [4]]
```

保全ルール:
- **元の actions 順序を維持**: 後段は前段の出力列を参照する
- **空ノード（actions=0）は新 .tfl に含めず削除**
- **分割後の前段・後段リワイヤ**: 元 SuperTransform の `previousNodes` / `nextNodes` を 2 つの新ノード両端に張り直す

### 不要フィールドの除去

新 .tfl では:
- `nodes`: 抽出ノード ＋ 新規 Input/Output のみ残す
- `connections`: 該当する接続のみ残す
- `name`: 新 .tfl の名前に変更
- `loomVersion` 等のメタは保持（ユーザーの Tableau Prep バージョンに合わせる）

### zip 化して .tfl 保存

```python
from flow_io import load_aux_entries, pack_flow_json, PUBLISHABLE_AUX_ENTRIES

aux_all = load_aux_entries(src_path)
aux = {k: aux_all[k] for k in PUBLISHABLE_AUX_ENTRIES if k in aux_all}
pack_flow_json(new_flow_json, output_path, aux_entries=aux)
```

`aux_entries` を省略すると `flow` だけの zip になり publish 拒否 (→ 280003)。`maestroMetadata` / `displaySettings` の引き継ぎ規約は冒頭の「`.tfl` の zip 内 entry (詳細)」節参照。

`.tflx` を作る場合は `.hyper` ファイルも zip 内に含める。MVP は `.tfl`（JSON のみ）で十分。

## 接続定義 (DB 直接接続の例)

```json
"<conn-id>": {
  "connectionType": "snowflake",
  "server": "...",
  "dbname": "...",
  "schema": "...",
  "username": "..."
}
```

Tableau Server 上の Published Data Source / 仮想接続を参照する形 (LoadSqlProxy 経由) は上の「切れた依存を新 Input ノードに置換」節を参照。

## .tfl と .tflx の使い分け

| 用途 | 推奨 |
|---|---|
| git 管理する原本 | `.tfl`（軽い、JSON のみ、diff 可能） |
| 動作確認・受け渡し | `.tflx`（extract 含むので即動く） |
| Tableau Server / Cloud への publish | どちらでも可 |

## バージョン互換

- **下位互換**: 新しい Tableau Prep で古い .tfl を開ける（自動アップグレード）
- **上位互換なし**: 古い Tableau Prep で新しい .tfl は開けないことがある

build 時に生成する .tfl は **ユーザーの Tableau Prep バージョンに合わせる**（出力時に `loomVersion` を指定）。

## 未知のノード種別への対処

新しい Tableau バージョンで未知の `nodeType` が出る可能性:

- extractor: `unknown` ラベルで Warnings に記載、処理続行
- tableau-prep-architect analyze: レイヤ推定保留（要判断としてマーク）
- tableau-prep-builder build: 元のノード定義をそのまま転写（変更しない）

判断に迷ったら **中断してユーザーに報告**。
