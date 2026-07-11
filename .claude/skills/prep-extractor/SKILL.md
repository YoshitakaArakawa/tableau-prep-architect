---
name: prep-extractor
description: Tableau Prep の .tfl / .tflx / flow.json およびサーバー上のプロジェクト階層を読み、後段が直接 JSON / REST を見なくて済むコンパクトなサマリに再構成する Skill。flow extraction（flow-summary.md）/ cloud context extraction（deploy-context.md + input-dispatch-mech.json）の 2 つのフェーズを持つ。大きな Prep フロー（数十〜数百ノード）を解析・分解する前、または Tableau Cloud に publish する前に必ず実行する。prep-architect の analyze/decompose、prep-deployer の preflight/publish の前段の前処理。Phase B は target_path walk + Input ノード kind 分類 + PDS LUID 解決を 1 フェーズに集約し、mechanical findings のみを返す (policy 提案 / rename 翻訳 / ユーザー確認は architect Stop 2 に集約)。ユーザーが「フローを extract して」「flow-summary を作って」「publish 先のプロジェクトを確認して」「Input を分類して」と言ったときに起動。サーバー上のフローを DL したい場合もここから（list_flows.py / download_flow.py）。
context: fork
model: claude-haiku-4-5-20251001
allowed-tools: Read Write Bash(python *) Bash(mkdir *) Glob Grep
---

# prep-extractor

Tableau Prep のフロー定義 JSON および Tableau Server/Cloud のプロジェクト階層を読み、後段の Skill が **flow.json や REST API を直接叩かなくて済む** コンパクトな markdown / JSON サマリを生成する Skill。**読み取り専用**（書き込み副作用は無い）。

## フェーズ

| フェーズ | 入力 | 出力 | スクリプト |
|---|---|---|---|
| **A: Flow extraction** | `.tfl` / `.tflx` / `flow.json` | `flow-summary.md` | `gen_flow_summary.py` (5 セクション一括生成) |
| **B: Cloud context extraction** | target path / LUID + `flow.json` (Phase A 出力) | `deploy-context.md` + `input-dispatch-mech.json` | `get_project_structure.py` + `dispatch_inputs.py` |

Phase A は独立に呼べる。Phase B は内部で `get_project_structure.py` と `dispatch_inputs.py` を順次回し、target_path walk + Input PDS 親プロジェクトの自動 `--also-scan` 解決 + Input ノード kind 分類 + PDS LUID 解決を 1 フェーズで完結させる。decompose 時点で `flow-summary.md` + `deploy-context.md` + `input-dispatch-mech.json` の 3 ファイルが揃っているのが理想。

## Caller から渡される入力

`context: fork` で動くため caller (メインエージェント) は会話履歴を渡せない。起動時に以下を文章で明示すること:

| 入力 | 必須 | 例 |
|---|---|---|
| `phase` | ✅ | `A` (flow extraction) / `B` (cloud context) / `all` |
| `input_path` | Phase A で必須 | ローカル `.tfl/.tflx/flow.json` のパス。サーバー DL から始める場合は flow 名 / LUID |
| `target_path` | Phase B で必須 | publish 先 target のフルパス。LUID 指定時は `target_luid` |
| `flow_json_path` | Phase B で必須 | Phase A が展開済の `flow.json` のパス (Input 分類 + PDS 親プロジェクト集合の自動算出に使用) |
| `output_path` | ✅ | `flow-summary.md` (Phase A) / `deploy-context.md` + `input-dispatch-mech.json` (Phase B) の出力先 (典型: `work/<yyyymmdd>_<tag>/reports/`)。MD/JSON レポートは [CLAUDE.md §work/ ディレクトリ規約](../../../CLAUDE.md#work-ディレクトリ規約) の `reports/` に集約 |

target_path の自然言語解釈は caller 側の責務。本 Skill は確定済み path のみ受ける。

## なぜこの Skill が独立しているか

- raw JSON は数百ノード規模で数 MB になることもあり、メイン会話のコンテキストを圧迫する
- `context: fork` で動くため、JSON 読み込みのコンテキスト肥大は主会話に波及しない
- 後段 Skill（prep-architect）は **flow-summary.md / deploy-context.md / input-dispatch-mech.json のみを入力** とすればよく、責務分離が明確になる
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

4 ステップ (入力展開 → `gen_flow_summary.py` 実行で 5 セクション一括生成 → 生成結果レビュー → 完了報告) で構成。セクションの手組みはしない。詳細手順・エラーハンドリング・サーバー DL の補助コマンド・制約は [references/flow-extraction-procedure.md](references/flow-extraction-procedure.md) を Read で取得。

主要参照: [tfl-json-schema.md](../../../references/tfl-json-schema.md) (JSON 構造 + UI⇔nodeType / actions マッピング) / [references/flow-summary-format.md](references/flow-summary-format.md) (出力書式)。

---

# Phase B: Cloud context extraction

Tableau Server/Cloud 上の **publish 先プロジェクト階層** を REST API で読み取り、**Prep flow の Input ノード分類 + PDS LUID 解決** も併せて行うフェーズ。**読み取り専用**、副作用なし。**ユーザー確認は持たない** (policy 提案 / rename 翻訳 / provisioning 確認は architect Stop 2 に集約)。

## なぜこのフェーズが必要か

- decompose（分解設計）時点で **既存 flow 名との衝突** を避けたい
- prep-deployer の preflight が「サブプロジェクト不足 → 作成承認を取る」判断材料を必要とする
- publish 時に「サインインしたユーザーがそもそも書き込み権限を持たない site / project だった」という遅発の事故を防ぐ
- **URL ID (`/projects/1117306` の数値) からの LUID 解決は REST 標準では不可能** なので、`Parent/Child` path での解決ロジックをここに集約
- Input ノード分類 (`flow_io.inspect_input_node`) と PDS LUID 解決 (deploy-context.md scan) は同じ「Cloud 状態スナップショット」の一部で、両方とも deploy-context.md に依存する mechanical 処理。architect (解釈 Skill) ではなく extractor (読み取り Skill) に置く方が責務対称
- 「整形済 PDS なので passthrough」「raw vconn なので augment」といった **policy 判断は業務知識依存 ([feedback_no_auto_detect_business_params] 系)** で本 Skill の責務外。architect に集約

## 入力

| 入力 | 扱い |
|---|---|
| target path（深さ自由、例: `"99_Sandbox/Q4-2026/flow241407_decompose"`） | top-level から `parent_id` チェーンを walk。途中で見つからないセグメントは pending |
| または target LUID | `server.projects.get_by_id` で直接取得、parent chain を逆走して existing prefix を再構成 |
| `flow.json` (Phase A 出力) | Input 分類 + Input PDS 親プロジェクト集合の自動算出 |
| `.env`（Repo 直下 or ユーザー作業フォルダ） | `SERVER`, `SITE_NAME` (OAuth ブラウザサインインで認証、secret は持たない) |

加えて出力先 (`deploy-context.md` + `input-dispatch-mech.json`) のパス。

## 出力

メイン会話への戻り値の末尾に **`## Timing` ブロック** を必ず含める ([references/skill-timing-contract.md](../../../references/skill-timing-contract.md))。Phase B の breakdown 推奨項目: `project tree fetch (1-pass)` / `dispatch classify (1-pass)` / `also-scan rescan` / `dispatch classify (final)` / `write`。

**2 ファイル**:

- `deploy-context.md` — Cloud project hierarchy (frontmatter + 8 セクション)。書式詳細 / 階層モデル (target = stg/int/marts の直上) / URL ID (vizportalUrlId) の LUID 逆引き不可問題 / 制約は [references/cloud-context-procedure.md](references/cloud-context-procedure.md)
- `input-dispatch-mech.json` — 各 Input ノードの kind 分類 + LUID 解決 + vconn metadata + fields[]。書式は [references/input-dispatch-format.md](references/input-dispatch-format.md)

自然言語による path 指示の path 化は **caller (メインエージェント) の責務**。本 Skill は確定済み path のみ受ける ([CLAUDE.md](../../../CLAUDE.md) Session intake Q4 補足参照)。

## 手順 (3 ステップ)

Phase B は内部で 3 ステップを順次実行する:

1. **1-pass target_path scan** — `get_project_structure.py` を target_path のみで実行し `deploy-context.md` 初版を書く
2. **Input dispatch + 親プロジェクト集合の抽出** — `dispatch_inputs.py` を flow.json + Step 1 の deploy-context.md で実行。出力 JSON の `pds_project_parents_needed_in_scope` から target_path 配下に無い親プロジェクトを抽出
3. **必要なら再 scan** — Step 2 で得た親プロジェクトを `--also-scan` で渡して `get_project_structure.py` を再実行し `deploy-context.md` を上書き → `dispatch_inputs.py` も再実行して PDS LUID を確定

Step 1 の親プロジェクト集合が空 (= 全 PDS Input が target_path 配下) なら Step 3 はスキップ。

CLI 例 / エラーハンドリング / unknown 検出時の挙動 / failure return paths は [references/cloud-context-procedure.md](references/cloud-context-procedure.md) を Read で取得。

## unknown 検出時の挙動

`flow_io.inspect_input_node` が kind=`unknown` を返した Input が 1 件以上あれば `dispatch_inputs.py` は exit 2。これは Skill 前提崩壊サイン (Prep version 差 / 壊れた flow.json) で、architect 以降を回しても half-defined な plan しか出せない。caller は exit 2 を受けたらユーザーに「flow_io 改修待ち」を伝えて session を中断する。

direct_db / extract は session 中断しない。architect が `## Input provisioning required` セクションで Cloud 整備案を提示し、build 時に当該 stg を skip するセマンティクスで進行する。

---

## 後段への引き渡し

| 後段 Skill | 渡すファイル |
|---|---|
| prep-architect (analyze / decompose) | `flow-summary.md` + `deploy-context.md` + `input-dispatch-mech.json` |
| prep-builder | `decomposition-plan.md`（prep-architect 出力） |
| prep-deployer (preflight) | `deploy-context.md` |
| prep-deployer (publish) | `flows/**/*.tfl` + `flows/staging/*.augmenter.json` + `deploy-context.md` |

後段 Skill は flow.json や REST API を **直接叩かず**、本 Skill の出力 markdown / JSON のみを読む。
