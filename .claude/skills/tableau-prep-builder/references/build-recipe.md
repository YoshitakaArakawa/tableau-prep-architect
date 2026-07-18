---
purpose: tableau-prep-builder の Build フェーズの変換規則の正典 — build_from_plan.py が実装する plan.json → .tfl 変換の仕様と、スクリプトが中断したときに LLM が個別対処するための手動手順
note: 設計案パース → 元 .tfl 展開 → ノード抽出・接続再構成 → zip 化 → verify → manifest init の変換規則を規定。通常の build は build_from_plan.py 1 コマンドで完結し (SKILL.md §手順)、本ファイルの手動手順は fallback。条件付き分岐は live-pds-augmenter-recipe.md (Materialization=live_pds) と special-outputs-recipe.md (Rename-back / incremental) に分離 — plan に該当 entry があるときだけ読む
---

# build-recipe

`tableau-prep-builder` Skill の **Build フェーズ** の変換規則。通常の build は [scripts/build_from_plan.py](../scripts/build_from_plan.py) が plan.json ([plan-json-schema.md](../../../../references/plan-json-schema.md)) から本ファイルの規則どおりに機械実行する — LLM が本ファイルの手順を手で回すのは **build_from_plan.py が中断したケースの個別対処のみ**。

## 目次

- 大原則 / 入力 / 手順概要
- Step 1: 設計案パース
- Step 2: 元 .tfl の展開 (Container 正規化 / aux entries)
- Step 3: ノード抽出と接続再構成 (3-pre passthrough / 3a 抽出 / 3b LoadSqlProxy 置換 / 3c actions 分割 / 3d Output / 3e 整理)
- Step 4: zip 化 / Step 4.5: verify (lineage + namespace) / Step 4.6: manifest 初期化
- Step 5: サマリ / エラーハンドリング / 検証順序

## 大原則

[SKILL.md](../SKILL.md) の設計原則 (元 .tfl 不変更 / 再 build 上書き許容 / 単体動作可能 / 失敗時中断) を前提とする。加えて:

- **build 前後で必ず Lineage closure check を回す** ([scripts/flow_io.py](../../../../scripts/flow_io.py) の `verify_lineage_closure`)。decompose の取り違え (= ステップを誤ったレイヤ / 誤った Input branch に配置) を機械的に検知

## 入力

- tableau-prep-architect の decompose の出力（`decomposition-plan-<flow>.json`、スキーマは [plan-json-schema.md](../../../../references/plan-json-schema.md)。md はレビュー用のレンダリング産物で build 入力ではない）
- 元の .tfl / .tflx（ノード定義の抽出元）
- ユーザー作業フォルダのパス（`flows/` を作る場所）

## 手順概要

```
1. plan.json を検証 (構造 + 元フロー整合 + 配線 + lineage closure) — scripts/plan_model.py
2. 元 .tfl を unzip → flow JSON を取得 (Container 正規化 + aux entries 確保)
3. 各 plan entry を kind dispatch で振り分け:
   - input_status=needs_provisioning → build skip (manifest に記録、成果物なし)
   - kind=pds_augment (stg のみ) → augmenter spec を emit
     (→ live-pds-augmenter-recipe.md を Read)
   - kind=tfl → .tfl を組む (Step 3a-3e)
4. zip 化 → verify (Step 4.5) → manifest init (Step 4.6)
5. 生成サマリを報告
```

## Step 3-pre: int / marts entry の Inputs 解決 (passthrough 対応)

int / marts の `Inputs` リストには 2 種類の PDS 参照が混在しうる:

1. **decompose 生成済 stg PDS** — `Published DS: stg_<name>` のように plan の stg entry 名を参照。`<target>/datasources/stg/<name>` で publish される前提。tableau-prep-deployer が同セッション内で publish するため build 時点では存在チェック不可
2. **passthrough from source flow** — `Published DS: <source-pds-name>` + `Project path: <path>` + `LUID: <luid>` の 3 行で記述された、元 flow から直接引き継ぐ PDS。architect Stop 2 確認時点で既存

builder は両方を **同じ LoadSqlProxy ノード** で表現する (Step 3b の `add_pds_input` + `wire_new_input_to_child` パターンと同一)。違いは `project_name` の与え方だけ — Case 1 は `ctx["ds_projects"]["staging"]["path"]` (フルパス)、Case 2 は plan の Project path を **そのままフルパスで** (leaf 名にしない、下記 ⚠️)。

**build 時の実在検証** (passthrough のみ): plan に明記された LUID が `deploy-context.md` の Datasources in scope に存在するか grep で確認。不在なら build 中断 (plan が古い or Phase B の `--also-scan` が足りない)。

> ⚠️ passthrough Input の `projectName` (LoadSqlProxy の connectionAttributes) には plan の Project path を **フルパスで** 書く (leaf 名にしない)。理由: deployer の dbname patch (`discover_pds_dbname.py --patch` / `auto_patch_downstream.py`) は LoadSqlProxy を `projectName` の **完全一致** で引き当てるため、leaf だと patch が 0 件マッチで silent に空振りし、dbname が placeholder のまま run 失敗する。フルパスの `projectName` は Cloud で publish/run 可能 — upstream_pds 側は元々フルパスで publish/run 実績があり、両分岐とも同じ `add_pds_input` を通る。Cloud から DL した既存フローでは Tableau が leaf のみを serialize することがあるが、builder が新規合成する LSP ではフルパスに統一する (LUID 解決も project_path 全体で行い、両者フルパスで一貫)。
>
> ⚠️ passthrough Input の `dbname`: plan に `dbname` があればそれを焼き込む (deployer の patch 往復が不要になる)。無ければ builder は placeholder を emit して WARNING を出し、deployer の `auto_patch_downstream` に委ねる (projectName がフルパスなので今は正しくマッチする)。plan には可能な限り input-dispatch の `pds.dbname` を入れる。

## Step 3-a: Materialization=live_pds の augmenter spec 組み立て

plan の stg entry に `Materialization: live_pds` がある場合のみ、[live-pds-augmenter-recipe.md](live-pds-augmenter-recipe.md) を Read して従う (.tfl を作らず `flows/staging/<name>.augmenter.json` を emit。kind 再検証・Transforms 表 op 検証・spec 組み立てを含む)。

## ステップ詳細

### Step 1: 設計案パース

plan.json を [scripts/plan_model.py](../../../../scripts/plan_model.py) の `load_plan` + `validate_plan_with_source` で検証し、`StepResolver` で step 番号 → node UUID を解決する (番号は flow-summary Topology と同じ `bfs_order` 採番)。各 entry の `included_steps` / `splits` / `inputs` / `output` / `layer` が build の入力。配線は `compute_flow_graph` が導出する ([plan-json-schema.md §配線の導出規則](../../../../references/plan-json-schema.md))。

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

Container を含まない (既に flat) フローでは no-op なので無条件に呼んでよい。

⚠️ `aux_for_new` を握り続けて Step 4 の `pack_flow_json` に渡すこと。これを忘れると `flow` だけの zip になり Server publish が拒否される (→ 280003)。

### Step 3: ノード抽出と接続再構成

各新 .tfl について:

#### 3a. 該当ノードと接続を抽出

各ノードは `copy_source_node` 経由で取得する。素の `copy.deepcopy(original["nodes"][nid])` ではなく **必ず `copy_source_node` を使う**。理由: 後者は `nextNodes` の枝刈り (kept_children 外を削除) と `nextNamespace` の verbatim 保持を同時に行う。`nextNamespace` を手で書き換えると Union 入力の namespaceFieldMappings や Join の Left/Right 識別が壊れ、run 時に "Union step is missing a connection" / "missing field on left side" が発生する。

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

元の依存（外部に出る前段）を **新規 `LoadSqlProxy` ノード** で置き換える (= 上流レイヤの Published Data Source を読む形)。LoadSqlProxy 一択の理由・Server 接続 dedup・dbname の罠などの構造仕様は [../../../../references/tfl-json-schema.md §切れた依存を新 Input ノードに置換](../../../../references/tfl-json-schema.md#切れた依存を新-input-ノードに置換-推奨-loadsqlproxy--上流-pds) を正典とする。

`add_pds_input` で Server 接続 / dataConnection 登録 / LoadSqlProxy ノード生成を一括実施。**子へのエッジ追加は `wire_new_input_to_child` で別途行う** — `add_pds_input(next_nodes=[...])` で生エッジを書くと `nextNamespace` を Default にハードコードしてしまう罠を避けるため:

```python
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from flow_io import add_pds_input, wire_new_input_to_child
from plan_model import parse_deploy_context

# parse_deploy_context は {server, site_luid, target_path, target_luid,
#   flow_projects:{layer:{path,luid}}, ds_projects:{layer:{path,luid}}} を返す。
# layer キーは "staging" / "intermediate" / "marts"。preflight 前で未作成のレイヤは
# dict から欠落する (skeleton 生成側が to-fill marker として表出)。
# site_url_name (content-url) は deploy-context に無い (frontmatter `site` は LUID) ので
# .env SITE_NAME から取る (plan_model.parse_deploy_context docstring 参照)。
ctx = parse_deploy_context("deploy-context.md")

lsp_id, _ = add_pds_input(
    new_flow,
    server_url=ctx["server"],
    site_url_name=os.environ["SITE_NAME"],
    project_name=ctx["ds_projects"]["staging"]["path"],  # 上流 PDS は datasources 配下 (例 "99_Sandbox/.../datasources/stg")
    datasource_name="stg_snowflake__orders",             # 上流 flow 名 = PDS 名
    dbname=None,        # build 時は None で OK (run 前に deployer が patch する契約)
    fields=upstream_fields_if_known,                     # 不明なら []
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

> ⚠️ **LSP が Union / Join の入力を置き換えるとき** は `replaced_source_parent_id` の指定が必須。これがないと source の `Union-Namespace-<uuid>` / `Left` / `Right` が継承されず Default に潰れ、run 時に Union/Join 接続エラーになる。Step 4.5 の `verify_edge_namespaces` で fail-fast 検知できるが、入口で正しく張ること。

#### 3c. actions 単位の分割実装

設計案の `## Actions-level splits` セクションに対象ノードがある場合、1 つの SuperTransform を 2 つ以上の新規 SuperTransform に分割する。実装パターン・保全ルール（順序保持、空ノード削除、リワイヤ）は [../../../../references/tfl-json-schema.md §SuperTransform を actions 単位で分割](../../../../references/tfl-json-schema.md#supertransform-を-actions-単位で分割) 参照。

#### 3d. Output ノードの追加 (全レイヤ PublishExtract → datasources/<layer> project)

全レイヤで `PublishExtract` を採用し、PDS は `<target>/datasources/<layer>` に書く (flow .tfl 自体の publish 先 `<target>/flows/<layer>` とは別プロジェクト、[project-hierarchy.md](../../../../references/project-hierarchy.md))。`WriteToHyper` (ローカル書き出し) は Cloud で下流から参照できないため使わない (Output ノード種別の一覧は tfl-json-schema.md)。

```python
from flow_io import make_publish_extract_node

layer = "staging"  # or "intermediate" / "marts" (plan.json のレイヤ名)
out_node = make_publish_extract_node(
    project_name=ctx["ds_projects"][layer]["path"],   # PDS publish 先 (例 "99_Sandbox/.../datasources/stg")
    project_luid=ctx["ds_projects"][layer]["luid"],   # 同上の LUID (preflight が作成、Phase B 再実行済み deploy-context.md に記載)
    datasource_name=new_flow_name,                    # flow 名 = PDS 名
    server_url=ctx["server"],
    site_url_name=os.environ["SITE_NAME"],
)
new_flow["nodes"][out_node["id"]] = out_node
# 末端ノードの nextNodes に out_node の id を追加するのを忘れない
```

`projectLuid` は publish 後の取り違え事故を避けるため必須。通常経路では preflight → Phase B 再実行 (migration-workflow step 4) で datasources 配下の layer project LUID が deploy-context.md に埋まり、gen_plan_skeleton がそれを plan.json の `ds_projects.<layer>.luid` に転記する — build_from_plan.py は plan.json から読む。上の手動 fallback では parse_deploy_context で Phase B 再実行済み deploy-context.md を直接読む。

ctx の project 構造 (parse_deploy_context の戻り値):

```python
ctx["flow_projects"][layer] = {"path": "<target>/flows/<layer>",       "luid": "<flows-layer-luid>"}        # .tfl publish 先 (publish_flow.py が使う)
ctx["ds_projects"][layer]   = {"path": "<target>/datasources/<layer>", "luid": "<datasources-layer-luid>"}  # PDS publish 先 (LSP Input & PublishExtract が使う)
# layer ∈ {"staging", "intermediate", "marts"}。preflight 前 (未作成) の layer は dict から欠落する
```

#### 3d-2 / 3d-3. Rename-back ノード挿入 / incremental 継承 (条件付き)

plan の mart entry に `Rename-back` 表がある場合、または元フローが incremental/append の場合のみ、[special-outputs-recipe.md](special-outputs-recipe.md) を Read して従う。該当 entry が無ければ読まなくてよい。

#### 3e. 不要フィールドの除去

新 .tfl に残すもの (`nodes` = 抽出ノード + 新規 Input/Output のみ / `connections` = 該当接続のみ / `name` = 新名 / `loomVersion` 等メタ保持) は [../../../../references/tfl-json-schema.md](../../../../references/tfl-json-schema.md) の該当節を正典とする。

### Step 4: zip 化して .tfl 保存

```python
from flow_io import pack_flow_json
pack_flow_json(
    new_flow_json,
    "flows/intermediate/int_orders_enriched.tfl",
    aux_entries=aux_for_new,   # Step 2 で取得した maestroMetadata + displaySettings
)
```

`aux_entries=` を渡さないと publish 不能 (→ 280003)。`.tflx` を作る場合は `.hyper` ファイルも zip 内に含める。MVP は `.tfl`（JSON のみ）で十分。

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
- **Lineage break**: ステップが宣言 Input から到達不能 = decompose の取り違え。例: Clean 4 (`Symbol = TRIM(...)`) を Transactions branch の stg flow に誤配置 → source DAG では Clean 4 は別 Input の子孫で到達できない → `lineage break: node 0ffb5436 ('Clean 4')`
- **Namespace mismatch**: parent→Union/Join のエッジで `nextNamespace` が Default に潰れている = namespace 喪失バグの silent failure を build 段階で fail-fast

検知されたら build を中断して decompose に差し戻す (該当ステップを正しいレイヤ / .tfl に再配置)。自動修復はしない (誤配置の意図解釈は危険)。原理は [../../../../references/decomposition-plan-format.md](../../../../references/decomposition-plan-format.md) の Lineage closure invariant 節。

### Step 4.6: publish-manifest 初期化 / 保持

全 .tfl を書き出して検証を通したら、session manifest を扱う。**既存の `publish-manifest.json` が `--output` 位置にあれば、原則保持** (上書きしない)。`init` は新規セッション (manifest 不在) のときだけ呼ぶ。

```bash
python scripts/publish_manifest.py init \
  --plan-json <session>/reports/decomposition-plan-<flow>.json \
  --flows-dir <session>/flows \
  --output <session>/reports/publish-manifest.json
```

(build_from_plan.py に `--manifest` を渡せばこの呼び出しは自動で行われる。旧セッションで md しか無い場合のみ legacy 形式 `--decomposition-plan <md> --flow-summary <flow-summary.md>` を使う — flow-summary に `- Outputs: N (...)` Meta 行が必要)

挙動:

- `--output` 既存ファイルなしの場合: `publish-manifest.json` を `status="pending"` / `luid=null` で新規生成 (plan.json の original / source_original_output_name / needs_provisioning entry + `flows/` スキャン結果をマージ)
- `--output` 既存ファイルありの場合: **デフォルトで exit 1 + エラーメッセージ** (publish/run 状態を失わないため)。再 build であってもこの保護は有効

original flow LUID は plan.json の `original.flow_luid` から取られる (null でも `resolve-luids` フェーズで名前から逆引きされる)。

**既存 manifest を意図的に上書きしたいとき** (Output mapping 変更・.tfl 増減など構造自体の再生成) のみ `--force` を付ける。publish/run 状態は完全に失われるので、事前に `cp publish-manifest.json publish-manifest.json.bak` を取る。再 build したが Output mapping は変えていない場合は `--force` を付けない。

manifest format は [../../../../references/publish-manifest-format.md](../../../../references/publish-manifest-format.md)。

### Step 5: サマリ出力

```markdown
## Build summary

Generated 7 new .tfl files:
  - flows/staging/stg_salesforce__opportunities.tfl
  - ...

Actions-level splits applied (2 ノード):
  - 元 Clean 1: Rename×4 を stg__transactions に、ROW_NUMBER LOD を int_orders_enriched に振り分け

Next steps for the user:
  1. Tableau Prep Builder で各 .tfl を開いて単体動作を確認
  2. 動作確認後、tableau-prep-deployer の publish へ進む (session intake で goal=④ なら自動継続)
```

## エラーハンドリング

| エラー | 挙動 |
|---|---|
| 設計案 markdown のパースに失敗 | 中断、形式エラーを報告 |
| 元 .tfl の zip 展開に失敗 | 中断、ファイル破損の可能性 |
| 未知の nodeType を含むノードを抽出 | そのまま転写（中身は変えない）＋ ユーザー警告 |
| 抽出ノード群に循環依存 | 中断、設計案の DAG を再確認するよう報告 |
| `flows/<layer>/<name>.tfl` が既存 | 上書きして再生成 (plan.json 由来の派生物なのでエラーではない、targeted fix `--only` を含む)。session manifest は Step 4.6 のとおり保持 |
| Output ノードを追加できない（型情報不足） | その .tfl をスキップ、レポートに「skipped: missing schema info」 |
| zip 書き込みに失敗 | 中断、ディスク容量・権限を確認するよう報告 |

## 検証順序 (レイヤ依存を踏まえた build/validate)

build 完了後の検証 (= publish 試行) には **レイヤ順依存** がある — int/marts は上流レイヤの PDS が Cloud 上に存在しないと `Input data source not found` で弾かれる。build は全レイヤ一括で行い、publish/run は stg → int → marts の順に 1 レイヤずつ完走させる (手順と並列化の粒度は [publish-recipe.md](../../tableau-prep-deployer/references/publish-recipe.md))。手動検証の項目は [SKILL.md §検証](../SKILL.md)。

## 実装方針

組み立てロジックは [scripts/build_from_plan.py](../scripts/build_from_plan.py) (決定論的 CLI) が実行する。LLM が [scripts/flow_io.py](../../../../scripts/flow_io.py) / [scripts/build_helpers.py](../../../../scripts/build_helpers.py) を直接叩くのは、スクリプトが中断したケース (非変換 Container、未知の nodeType への個別対処等) のみ。

## 参考

- .tfl JSON スキーマ・組み立てパターン: [../../../../references/tfl-json-schema.md](../../../../references/tfl-json-schema.md)
- 命名規約: [../../../../references/naming-conventions.md](../../../../references/naming-conventions.md)
- レイヤ責務: [../../../../references/layer-responsibilities.md](../../../../references/layer-responsibilities.md)
- decompose の出力書式: [../../../../references/decomposition-plan-format.md](../../../../references/decomposition-plan-format.md)
