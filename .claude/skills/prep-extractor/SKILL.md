---
name: prep-extractor
description: Tableau Prep の .tfl / .tflx / flow.json およびサーバー上のプロジェクト階層を読み、後段が直接 JSON / REST を見なくて済むコンパクトな markdown サマリに再構成する Skill。flow extraction（flow-summary.md）と cloud structure extraction（deploy-context.md）の 2 つのフェーズを持つ。大きな Prep フロー（数十〜数百ノード）を解析・分解する前、または Tableau Cloud に publish する前に必ず実行する。prep-architect の analyze/decompose、prep-deployer の preflight/publish の前段の前処理。ユーザーが「フローを extract して」「flow-summary を作って」「publish 先のプロジェクトを確認して」と言ったときに起動。サーバー上のフローを DL したい場合もここから（list_flows.py / download_flow.py）。
context: fork
allowed-tools: Read Write Bash(python *) Bash(mkdir *) Glob Grep
---

# prep-extractor

Tableau Prep のフロー定義 JSON および Tableau Server/Cloud のプロジェクト階層を読み、後段の Skill が **flow.json や REST API を直接叩かなくて済む** コンパクトな markdown サマリを生成する Skill。**読み取り専用**（書き込み副作用は無い）。

## フェーズ

| フェーズ | 入力 | 出力 | スクリプト |
|---|---|---|---|
| **Flow extraction** | `.tfl` / `.tflx` / `flow.json` | `flow-summary.md` | `inspect_actions.py` 等 |
| **Cloud structure extraction** | 親プロジェクト path / LUID | `deploy-context.md` | `get_project_structure.py` |

両フェーズは独立して呼べる（順序は問わない）。decompose 時点で両方揃っているのが理想。

## Caller から渡される入力

`context: fork` で動くため caller (メインエージェント) は会話履歴を渡せない。起動時に以下を文章で明示すること:

| 入力 | 必須 | 例 |
|---|---|---|
| `phase` | ✅ | `A` (flow extraction) / `B` (cloud structure) / `both` |
| `input_path` | Phase A で必須 | ローカル `.tfl/.tflx/flow.json` のパス。サーバー DL から始める場合は flow 名 / LUID |
| `target_path` | Phase B で必須 | publish 先 target のフルパス。LUID 指定時は `target_luid` |
| `output_path` | ✅ | `flow-summary.md` / `deploy-context.md` の出力先 (典型: `work/<yyyymmdd>_<tag>/reports/`)。MD レポートは [CLAUDE.md §work/ ディレクトリ規約](../../../CLAUDE.md#work-ディレクトリ規約) の `reports/` に集約 |

target_path の自然言語解釈は caller 側の責務。本 Skill は確定済み path のみ受ける。

## なぜこの Skill が独立しているか

- raw JSON は数百ノード規模で数 MB になることもあり、メイン会話のコンテキストを圧迫する
- `context: fork` で動くため、JSON 読み込みのコンテキスト肥大は主会話に波及しない
- 後段 Skill（prep-architect）は **flow-summary.md / deploy-context.md のみを入力** とすればよく、責務分離が明確になる
- Cloud 側構造取得もここに集約することで **「読み取り = extractor / 書き込み = deployer」** の役割対称性が成立

---

# Phase A: Flow extraction

`.tfl` / `.tflx` から `flow-summary.md` を生成する。

## 入力

| 入力 | 扱い |
|---|---|
| `.tfl` / `.tflx` ファイル | zip なので `flow` エントリを抽出して JSON を取得 |
| 既に展開済の `flow.json` | そのまま読み込み |

加えて、出力先の `flow-summary.md` のパス（典型的には `work/<yyyymmdd>_<summary>/reports/flow-summary.md`）。

サーバー上のフローを取得したい場合は scripts/list_flows.py で LUID を引き当てて scripts/download_flow.py で DL する。

## 出力

**`flow-summary.md`** ファイル 1 枚。書式は [references/flow-summary-format.md](references/flow-summary-format.md) に厳密に従う。

含めるセクション:

1. **Meta** — source path, flow name, total nodes, total actions, nodeType 構成
2. **Topology** — トポロジカル順のノード一覧表
3. **Dependency DAG** — Mermaid 形式の DAG 図
4. **SuperTransform actions inventory** — 各 SuperTransform の `beforeActionAnnotations` を 1 行サマリ化
5. **Warnings** — 未知 nodeType / action type、空ノード、同名ノード、孤立ノード等

メイン会話への戻り値の末尾に **`## Timing` ブロック** を必ず含める ([references/skill-timing-contract.md](../../../references/skill-timing-contract.md))。Phase A の breakdown 推奨項目: `input load (.tfl 展開)` / `topology 抽出` / `actions inventory` / `Mermaid 生成` / `write`。

## 手順

### Step 1: 入力ファイルの展開

`.tfl` / `.tflx` の場合は Repo 直下 [scripts/flow_io.py](../../../scripts/flow_io.py) の `unpack_flow_json` を使う:

```bash
python -c "
import sys; sys.path.insert(0, 'scripts')
from flow_io import unpack_flow_json
unpack_flow_json('<input.tfl>', 'work/<date>/flow.json')
"
```

### Step 2: 構造情報の確認

以下を Read して flow JSON の構造を把握する:

- [../../../references/tfl-json-schema.md](../../../references/tfl-json-schema.md) — ファイル形式、トップレベル JSON 構造、`previousNodes` の罠、`beforeActionAnnotations` ラップ構造、`initialNodes` BFS 規約
- [../../../references/prep-ui-to-json-mapping.md](../../../references/prep-ui-to-json-mapping.md) — UI ステップ ⇔ nodeType ⇔ actions サブタイプの対応表
- [references/flow-summary-format.md](references/flow-summary-format.md) — 出力書式の厳密仕様

### Step 3: トポロジ復元

LLM 自身が flow.json を読んで以下を抽出する（ノード数が多ければ Bash で支援してよい）:

1. `flow["initialNodes"]` をエントリーポイントとして BFS 順序を確定 → 短 ID `#1, #2, ...` を採番
2. 各ノードの `nextNodes[].nextNodeId` を抽出 → これを反転して各ノードの `Prev` を求める（`previousNodes` は空なので使わない）
3. `nodeType` の version prefix を剥がす（最後のドット以降を採用）
4. ノード名（`name`）と組み合わせて Topology テーブルを構築

BFS の擬似コードは [tfl-json-schema.md](../../../references/tfl-json-schema.md) 参照。

### Step 4: actions inventory 生成

各 SuperTransform について `beforeActionAnnotations` 配列を走査:

- 各要素は `{"annotationNode": {...}}` でラップされているので 1 階下ろす
- 内側 `nodeType` の末尾に応じて要約フォーマットを選ぶ
- 詳細フォーマットは [references/flow-summary-format.md](references/flow-summary-format.md) の actions inventory セクション参照

ノード数が多くて手動走査が辛い場合は支援スクリプトを使う:

```bash
python .claude/skills/prep-extractor/scripts/inspect_actions.py \
    work/<date>/flow.json \
    -o work/<date>/scratch/_actions-tmp.md
```

このスクリプトの出力はそのまま flow-summary.md の対応セクションに転記してよい。Topology テーブルや Mermaid DAG は LLM が直接生成する。

### Step 5: Mermaid DAG 生成

Topology の Next 列をベースに `graph TD` を出力。各ノードは `n<id>[#<id> <nodeType> <Name>]` 形式。分岐・合流が一目で分かるレイアウトを保つ。

### Step 6: Warnings 集約

走査中に検出した以下を Warnings セクションに列挙:

- 未知 nodeType
- 未知 action type
- `beforeActionAnnotations` が空 (`0 actions`) の SuperTransform — 削除候補
- 重複名のノード — build 時のファイル名衝突要注意
- 孤立ノード
- 循環依存の兆候
- **SuperUnion ノード — 全件必ず 1 行ずつ追加**: `🔒 Node #N <UnionName> (SuperUnion): injects implicit Table Names column — do NOT propose deletion`。actions=0 や branch 同一性に関わらず Union は schema 等価ではない (`Table Names` を暗黙注入する)。analyze / decompose 側で削除候補と判定するのを構造的に防ぐ二重防御

### Step 7: 書き出しと完了報告

`flow-summary.md` を指定パスに Write。ユーザーには以下を簡潔に報告:

- 出力パス
- 総ノード数 / SuperTransform 数 / 総 actions 数
- Warnings の件数（重要なものは 1-2 件抜粋）
- 次に推奨するアクション（例: 「prep-architect の analyze フェーズへ」）

## エラーハンドリング

| エラー | 挙動 |
|---|---|
| 入力ファイルが見つからない | 中断、パス確認をユーザーに依頼 |
| zip 展開失敗 (`.tfl` / `.tflx`) | 中断、ファイル破損の可能性を報告 |
| `flow["nodes"]` がない / 空 | 中断、Prep フローの JSON ではない可能性 |
| `initialNodes` が空 | Warnings に記載しつつ、ノード辞書の全件を topological 推定でフォールバック |
| 未知 nodeType / action type | **中断しない**、Warnings に記載してそのまま処理続行 |
| 出力先ディレクトリが存在しない | 親ディレクトリを `mkdir -p` で作成 |

## サーバーからの DL（任意のサブ手順）

```bash
# LUID 確認（URL の数値 ID は LUID ではない）
python .claude/skills/prep-extractor/scripts/list_flows.py --name-contains "<flow name>"

# DL
python .claude/skills/prep-extractor/scripts/download_flow.py \
    --flow-id <LUID> \
    --output work/<date>/source.tflx
```

認証は Repo 直下 [scripts/tableau_auth.py](../../../scripts/tableau_auth.py) が `.env` を読む（[CLAUDE.md](../../../CLAUDE.md) の認証情報の運用節参照）。

## 制約 (Phase A)

- **Tableau Prep のバージョンに依存**。新バージョンで構造が変わったら Repo 直下 [references/tfl-json-schema.md](../../../references/tfl-json-schema.md) と [references/prep-ui-to-json-mapping.md](../../../references/prep-ui-to-json-mapping.md) を更新する
- **業務的解釈・レイヤ推定はしない**（それは prep-architect analyze の役割）
- **分解設計もしない**（同 decompose の役割）
- 本 Skill は純粋に「構造の機械的抽出 + Mermaid 可視化」に専念する

---

# Phase B: Cloud structure extraction

Tableau Server/Cloud 上の **publish 先プロジェクト階層** を REST API で読み取り、後段が消費できる `deploy-context.md` を生成する。**読み取り専用**、副作用なし。

## なぜこのフェーズが必要か

- decompose（分解設計）時点で **既存 flow 名との衝突** を避けたい
- prep-deployer の preflight が「サブプロジェクト不足 → 作成承認を取る」判断材料を必要とする
- publish 時に「PAT がそもそも書き込み権限を持たない site / project だった」という遅発の事故を防ぐ
- **URL ID (`/projects/1117306` の数値) からの LUID 解決は REST 標準では不可能** なので、`Parent/Child` path での解決ロジックをここに集約

## モデル: target と任意深さの上位階層

publish 先構造は **「最下層は規約固定、それより上は柔軟」** とする:

```
<top-level>                  ┐
└── (任意の中間階層 0個以上)   │ ← この上位パスはユーザーごとに自由
    └── target               ┘   target = stg/int/marts の直上 (= publish 先プロジェクト群の親)
        ├── stg/             ┐
        ├── intermediate/    │ ← この 3 つは規約固定、prep-deployer が承認付き作成
        └── marts/           ┘
```

| 階層 | 例 | 責務 |
|---|---|---|
| **dbt layers** (固定) | `stg / intermediate / marts` | prep-deployer が承認付き作成 |
| **target** | `flow241407_decompose` / `Sales Analytics` / `v1` | publish 先 dbt 3 つの直上。存在しなくても良い |
| **上位の中間階層** | `99_Sandbox/Q4-2026/...` 等、ユーザー依存 | 存在するものは尊重、不足分は prep-deployer が承認付き作成 |

ユーザーが指定する path は **target のフルパス**（target までの全セグメント）。深さは何段でも良い:

- `"Sales Analytics"` — top-level プロジェクトを target に
- `"99_Sandbox/flow241407_decompose"` — sandbox 1 段 + target
- `"99_Sandbox/Q4-2026/decompose-X/v1"` — 中間 2 段 + target
- LUID 直指定もあり

`get_project_structure.py` は path を walk し、**存在する prefix（`existing_chain`）** と **作成すべき残り（`pending_segments`）** に分割する。後段の prep-deployer はそれをループで埋める。

自然言語による path 指示 (例: 「99_Sandbox の下に decompose 用のフォルダを作って」) の path 化は **caller (メインエージェント) の責務**。本 Skill は確定済み path のみ受ける ([CLAUDE.md](../../../CLAUDE.md) Session intake Q4 補足参照)。

## 入力

| 入力 | 扱い |
|---|---|
| target path（深さ自由、例: `"99_Sandbox/Q4-2026/flow241407_decompose"`） | top-level から `parent_id` チェーンを walk。途中で見つからないセグメントは pending |
| または target LUID | `server.projects.get_by_id` で直接取得、parent chain を逆走して existing prefix を再構成 |
| `.env`（Repo 直下 or ユーザー作業フォルダ） | `SERVER`, `SITE_NAME`, `PAT_NAME`, `PAT_VALUE` |

加えて出力先 `deploy-context.md` のパス。

## 出力

メイン会話への戻り値の末尾に **`## Timing` ブロック** を必ず含める ([references/skill-timing-contract.md](../../../references/skill-timing-contract.md))。Phase B の breakdown 推奨項目: `project tree fetch` / `parent walk + naming scan` / `write`。

**`deploy-context.md`** 1 枚。frontmatter:

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
7. **Next step** — prep-architect / prep-deployer への引き渡し

## 手順

```bash
# target が既存
python .claude/skills/prep-extractor/scripts/get_project_structure.py \
    --project-path "Sales Analytics" \
    -o work/<date>/reports/deploy-context.md

# 標準的なネスト 1 段、target は未作成
python .claude/skills/prep-extractor/scripts/get_project_structure.py \
    --project-path "99_Sandbox/flow241407_decompose" \
    -o work/<date>/reports/deploy-context.md

# 深いネスト、中間も未作成
python .claude/skills/prep-extractor/scripts/get_project_structure.py \
    --project-path "99_Sandbox/Q4-2026/decompose-X/v1" \
    -o work/<date>/reports/deploy-context.md

# LUID 直指定
python .claude/skills/prep-extractor/scripts/get_project_structure.py \
    --project-id <luid> \
    -o work/<date>/reports/deploy-context.md
```

スクリプトは:

1. 全プロジェクトを `server.projects.get()` で fetch（pagesize=1000、ページング対応）
2. path を `/` で分割し、top-level → leaf に向かって **1 セグメントずつ** `(parent_id, name)` で照合
3. 最初に存在しなかったセグメントとそれ以降を `pending_segments` に積む
4. ambiguity（同名複数）は ValueError（`--project-id` で解消）
5. target が存在すれば直下サブプロジェクトと subtree 内の flow を集計
6. frontmatter + sections を組み立てて Write

## URL ID 解決について

`https://<your-pod>.online.tableau.com/#/site/<contentUrl>/projects/<id>` の数値 ID は **vizportalUrlId** で、Tableau REST API の標準エンドポイント (`GET /sites/{site-id}/projects`) には **返らない**。よって `1117306` のような数値から LUID への直接マップは不可。代替手段:

- ユーザーに project name または `Parent/Child` path を聞く（本フェーズの基本動作）
- Metadata API (GraphQL) も `vizportalUrlId` を返さないため逆引き不可（検証済み）

## 制約 (Phase B)

- 読み取り専用 — サブプロジェクト作成や権限変更は **prep-deployer の preflight** に委譲
- `writeable` フィールドは TSC が PAT によっては populate しないため `unknown` で報告するケースあり（実体は publish 試行で確認）
- 同名 top-level プロジェクトが複数ある site では `--project-id` で曖昧性解消が必要

---

## 後段への引き渡し

| 後段 Skill | 渡すファイル |
|---|---|
| prep-architect (analyze / decompose) | `flow-summary.md` + `deploy-context.md`（あれば） |
| prep-builder | `decomposition-plan.md`（prep-architect 出力） |
| prep-deployer (preflight) | `deploy-context.md` |
| prep-deployer (publish) | `flows/**/*.tfl` + `deploy-context.md` |

後段 Skill は flow.json や REST API を **直接叩かず**、本 Skill の出力 markdown のみを読む。
