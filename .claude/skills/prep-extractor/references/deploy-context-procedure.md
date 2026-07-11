---
purpose: prep-extractor Phase B (server structure + input dispatch) の詳細手順。target_path walk による Tableau Server/Cloud project hierarchy 取得と、flow.json + deploy-context.md からの Input 分類 + PDS LUID 解決を 1 フェーズ内で完結させる
note: Phase B の責務範囲 (target_path walk / Input PDS 親プロジェクトの自動 --also-scan / kind 分類 / LUID 解決 / unknown raise) と 2 出力 (deploy-context.md + input-dispatch-mech.json) のオーケストレーションを規定する
---

# Phase B 実装手順 (deploy-context)

Phase B は Tableau Server/Cloud の **project hierarchy の取得** と **Prep flow の Input ノード分類 + PDS LUID 解決** を 1 フェーズで担う。出力は 2 ファイル: `deploy-context.md` (server structure) + `input-dispatch-mech.json` (Input dispatch、mechanical findings)。**ユーザー確認は持たない** (policy 提案 / rename 翻訳 / provisioning 確認は architect Stop 2 に統合)。

## 目次

- モデル: target と任意深さの上位階層 / 入力 / 出力
- 手順 (3 ステップ) / get_project_structure.py・dispatch_inputs.py の内部処理
- unknown 検出時の挙動 / Phase B 範囲外 (architect 責務) / URL ID 解決について
- 失敗時の戻り先 / 制約 / 後段への引き渡し

## モデル: target と任意深さの上位階層

publish 先階層のモデルは [project-hierarchy.md](../../../../references/project-hierarchy.md) を正典とする (target = `flows/`・`datasources/` の直上、各々の下に dbt 3 レイヤ、上位は任意深さ・任意命名)。ユーザーが指定する path は **target のフルパス** で、深さは何段でも良い (target LUID 直指定も可)。

`get_project_structure.py` は path を walk し、**存在する prefix（`existing_chain`）** と **作成すべき残り（`pending_segments`）** に分割する。後段の prep-deployer preflight はそれをループで埋める。

## 入力

| 入力 | 必須 | 扱い |
|---|---|---|
| target path（深さ自由、例: `"99_Sandbox/Q4-2026/flow241407_decompose"`） | ✅ | top-level から `parent_id` チェーンを walk。途中で見つからないセグメントは pending |
| または target LUID | (path 代替) | `server.projects.get_by_id` で直接取得、parent chain を逆走して existing prefix を再構成 |
| `flow.json` (Phase A 出力) | ✅ | Input 分類 + Input PDS 親プロジェクト集合の抽出に使用 |
| `.env`（Repo 直下 or ユーザー作業フォルダ） | ✅ | `SERVER`, `SITE_NAME` (OAuth ブラウザサインインで認証、secret は持たない) |
| 出力先 `deploy-context.md` / `input-dispatch-mech.json` のパス | ✅ | 典型: `work/<session>/reports/` 配下 |

## 出力

### `deploy-context.md`

frontmatter:

```yaml
target_path: 99_Sandbox/Q4-2026/decompose-X/v1
target_status: exists | pending
target_luid: <luid or null>
existing_prefix_path: 99_Sandbox       # 最深の既存セグメント。null = 全部 pending（top-level から）
existing_prefix_luid: <luid or null>
pending_segments:                      # 作成すべきセグメント列。target=existing なら []
  - Q4-2026
  - decompose-X
  - v1
```

本文セクション:

1. **Target (parent of stg/int/marts)** — path / LUID / status / writeable?
2. **Existing prefix** — 既存の最深 path と chain（root→leaf テーブル）
3. **Pending segments** — 作成順テーブル（`parent at creation time` 列付き）
4. **Subprojects directly under target** — target 直下（exists のときのみ）
5. **dbt layer presence** — `stg / intermediate / marts` 有無
6. **Existing flows in target subtree** — 名前衝突回避用
7. **Datasources in scope** — `target_path` 配下と `--also-scan` で追加された Input PDS 親プロジェクト配下の Published Datasource 一覧 (project_path / name / luid)
8. **Next step** — prep-architect / prep-deployer への引き渡し

### `input-dispatch-mech.json`

各 Input ノードの kind 分類 + LUID 解決 + vconn metadata + fields[] を含む mechanical findings。書式は [input-dispatch-format.md](input-dispatch-format.md)。

## 手順 (3 ステップ)

Phase B は内部で 3 ステップを順次実行する:

### Step 1: 1-pass target_path scan

target_path のみで `get_project_structure.py` を 1 回走らせて `deploy-context.md` 初版を作る。

```bash
python .claude/skills/prep-extractor/scripts/get_project_structure.py \
    --project-path "<target_path>" \
    -o <output_dir>/deploy-context.md
```

### Step 2: flow.json から Input PDS 親プロジェクト集合を抽出

`scripts/flow_io.py` の `inspect_input_node` を全 Input ノードに適用し、`kind=pds` の Input から `connectionAttributes.projectName` を集める。`dispatch_inputs.py` を **Step 1 の deploy-context.md** と組合せて 1 回走らせ、出力 JSON の `pds_project_parents_needed_in_scope` を読む方法でも同じ集合が取れる (こちらの方が実装簡潔)。

```bash
python .claude/skills/prep-extractor/scripts/dispatch_inputs.py \
    --flow-json <flow_json_path> \
    --deploy-context <output_dir>/deploy-context.md \
    --output <output_dir>/input-dispatch-mech.json
```

stdout の `RESULT_JSON:` 行 + 出力 JSON の `pds_project_parents_needed_in_scope` フィールドから親プロジェクト集合を取り出す。

### Step 3: 親プロジェクトが target_path 配下に無ければ再 scan

Step 2 で得た親プロジェクト集合のうち、Step 1 の `deploy-context.md` の `## Datasources in scope` に含まれていないものを `--also-scan` で渡して `get_project_structure.py` を再実行し、`deploy-context.md` を上書きする。

```bash
python .claude/skills/prep-extractor/scripts/get_project_structure.py \
    --project-path "<target_path>" \
    --also-scan "0_Datasource" \
    --also-scan "Shared_PDS" \
    -o <output_dir>/deploy-context.md
```

その後 `dispatch_inputs.py` を再実行して PDS LUID を確定:

```bash
python .claude/skills/prep-extractor/scripts/dispatch_inputs.py \
    --flow-json <flow_json_path> \
    --deploy-context <output_dir>/deploy-context.md \
    --output <output_dir>/input-dispatch-mech.json
```

> ⚠️ Step 3 をスキップすると PDS LUID が `unresolved` のまま architect に渡る。architect は `unresolved` PDS は passthrough 候補から外して Stop 2 で「再 scan するか / 当該 Input を augment 扱いに切り替えるか」をユーザーに提示する責務を持つ。

Step 1 の親プロジェクト集合が空 (= 全 PDS Input が target_path 配下の PDS を参照) なら Step 3 はスキップ可能。

## get_project_structure.py の内部処理

スクリプトは:

1. 全プロジェクトを `server.projects.get()` で fetch（pagesize=1000、ページング対応）
2. path を `/` で分割し、top-level → leaf に向かって **1 セグメントずつ** `(parent_id, name)` で照合
3. 最初に存在しなかったセグメントとそれ以降を `pending_segments` に積む
4. ambiguity（同名複数）は ValueError（`--project-id` で解消）
5. target が存在すれば直下サブプロジェクトと subtree 内の flow を集計
6. `--also-scan` 引数があれば追加で Published Datasource 一覧 (project_path / name / luid) を `## Datasources in scope` に書き出す
7. frontmatter + sections を組み立てて Write

## dispatch_inputs.py の内部処理 (mechanical)

| 項目 | 内容 |
|---|---|
| Input 分類 | `flow_io.inspect_input_node` で `pds / vconn / direct_db / extract / unknown` に振り分け |
| PDS LUID 解決 | deploy-context.md の `## Datasources in scope` 表をパース、(`projectName`, `datasourceName`) で照合。1 件一致 → `resolved`、複数 → `ambiguous` (candidates 列挙)、0 件 → `unresolved` |
| vconn metadata 抽出 | `resourceId` (vconn LUID) / `resourceName` / `relation.table` の bracket parse (table_uuid + table_name) / `fields[]` 一覧 |
| direct_db 情報 | base connection の `class` (snowflake / postgres / etc.) を抽出。architect が provisioning 案 (vconn 化 / PDS 化) を提示する材料 |
| fields 整理 | `isGenerated=True` を除外、`name_raw` / `name_bracketed` / `caption` / `datatype` を 1 列 1 オブジェクトに揃える |
| 追加スキャン要請 | flow.json 内の全 PDS Input の `projectName` 集合を `pds_project_parents_needed_in_scope` として emit |

スクリプト出力は **JSON 1 ファイル + stdout に `RESULT_JSON:` 行**。

## unknown 検出時の挙動

`flow_io.inspect_input_node` が kind=`unknown` を返した Input が 1 つでもあれば、`dispatch_inputs.py` は **exit code 2 で停止**。これは Skill 前提崩壊サイン (Prep version 差で nodeType 想定外 / dataConnections が壊れている等) で、architect 以降を回しても half-defined な plan しか出せない。

extractor は unknown を `needs_provisioning` 扱いで先送りしない:

- `needs_provisioning` (direct_db / extract) は **Cloud 整備で解決可能** な案件で plan に同梱する価値がある
- `unknown` は **判定不能** で plan を組む足場が無い (列スキーマも取れない可能性が高い)

caller (メインエージェント) は exit 2 を受けたらユーザーに「`flow_io.inspect_input_node` の改修要、Skill 更新待ち」を伝えて session を中断する。再開は flow_io の改修コミット後、Phase A から。

## Phase B 範囲外 (architect 責務)

以下は本フェーズでは扱わない。architect の decompose で `input-dispatch-mech.json` を読み込んで処理する:

| 項目 | architect での処理場所 |
|---|---|
| caption の semantic translation (`数量` → `quantity` 等) | decompose の Rename proposals 表 |
| policy 判定 (passthrough vs augment) | decompose の各 stg entry の `policy` フィールド |
| cast / hide 提案 | decompose の Transforms 表 |
| provisioning 案 (direct_db / extract → vconn 化 / PDS 化) | decompose の `## Input provisioning required` セクション |
| ユーザー確認 | Stop 2 (decomposition-plan 全体と一括) |

## URL ID 解決について

`https://<your-pod>.online.tableau.com/#/site/<contentUrl>/projects/<id>` の数値 ID は **vizportalUrlId** で、Tableau REST API の標準エンドポイント (`GET /sites/{site-id}/projects`) には **返らない**。よって `1117306` のような数値から LUID への直接マップは不可。Metadata API (GraphQL) も `vizportalUrlId` を返さないため逆引き不可。代替手段はユーザーに project name または `Parent/Child` path を聞く (本フェーズの基本動作)。

## 失敗時の戻り先

| 状況 | 対処 |
|---|---|
| `get_project_structure.py` で path 解決失敗 | path のスペル確認 / `--project-id` で LUID 直指定 / 認証 (`.env`) 確認 |
| `dispatch_inputs.py` で flow.json parse 失敗 | Phase A をやり直し |
| 全 PDS Input が unresolved | architect Stop 2 で「再 scan するか / 当該 Input を augment 扱いに切り替えるか」をユーザー確認 |
| 1 つ以上の Input が `unknown` | dispatch_inputs.py が exit 2、session 中断。flow_io 改修待ち |
| direct_db / extract が混在 | session 中断しない。architect が decomposition-plan に `## Input provisioning required` として同梱し、Stop 2 で整備依頼。build 時は当該 stg を skip + manifest warning |

## 制約 (Phase B)

- 読み取り専用 — サブプロジェクト作成や権限変更は **prep-deployer の preflight** に委譲
- `writeable` フィールドは TSC が認証ユーザーによっては populate しないため `unknown` で報告するケースあり（実体は publish 試行で確認）
- 同名 top-level プロジェクトが複数ある site では `--project-id` で曖昧性解消が必要

## 後段への引き渡し

引き渡し表は [SKILL.md §後段への引き渡し](../SKILL.md#後段への引き渡し) を正典とする。補足: prep-builder は `input-dispatch-mech.json` を直接参照する必要はない (architect が decomposition-plan に embed する) が、`Materialization=live_pds` 宣言の最終検証 (= 該当 Input が本当に vconn か) のみ flow_io で再確認する (silent fallback 禁止、build-recipe.md 参照)。
