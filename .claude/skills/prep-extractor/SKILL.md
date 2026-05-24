---
name: prep-extractor
description: Tableau Prep の .tfl / .tflx / flow.json およびサーバー上のプロジェクト階層を読み、後段が直接 JSON / REST を見なくて済むコンパクトな markdown サマリに再構成する Skill。flow extraction（flow-summary.md）/ cloud structure extraction（deploy-context.md）/ input dispatch (input-dispatch.md) の 3 つのフェーズを持つ。大きな Prep フロー（数十〜数百ノード）を解析・分解する前、または Tableau Cloud に publish する前に必ず実行する。prep-architect の analyze/decompose、prep-deployer の preflight/publish の前段の前処理。Phase C は各 Input の取扱方針 (passthrough / augment / block) を AI 提案 → ユーザー確認の形で確定する。ユーザーが「フローを extract して」「flow-summary を作って」「publish 先のプロジェクトを確認して」「Input の方針を決めて」と言ったときに起動。サーバー上のフローを DL したい場合もここから（list_flows.py / download_flow.py）。
context: fork
model: claude-haiku-4-5-20251001
allowed-tools: Read Write Bash(python *) Bash(mkdir *) Glob Grep
---

# prep-extractor

Tableau Prep のフロー定義 JSON および Tableau Server/Cloud のプロジェクト階層を読み、後段の Skill が **flow.json や REST API を直接叩かなくて済む** コンパクトな markdown サマリを生成する Skill。**読み取り専用**（書き込み副作用は無い）。

## フェーズ

| フェーズ | 入力 | 出力 | スクリプト |
|---|---|---|---|
| **A: Flow extraction** | `.tfl` / `.tflx` / `flow.json` | `flow-summary.md` | `inspect_actions.py` 等 |
| **B: Cloud structure extraction** | 親プロジェクト path / LUID | `deploy-context.md` | `get_project_structure.py` |
| **C: Input dispatch** | flow.json + deploy-context.md | `input-dispatch.md` (proposal → ユーザー確認後 confirmed) | `dispatch_inputs.py` |

Phase A / B は独立して呼べる (順序は問わない)。Phase C は A と B の両方の出力に依存するため、A → B → C の順序が事実上必要。decompose 時点で 3 ファイル全部揃っているのが理想。

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

7 ステップ (入力展開 → 構造把握 → トポロジ復元 → actions inventory → Mermaid DAG → Warnings 集約 → 書き出し) で構成。詳細手順・エラーハンドリング・サーバー DL の補助コマンド・制約は [references/phase-a-procedure.md](references/phase-a-procedure.md) を Read で取得。

主要参照: [tfl-json-schema.md](../../../references/tfl-json-schema.md) (JSON 構造 + UI⇔nodeType / actions マッピング) / [references/flow-summary-format.md](references/flow-summary-format.md) (出力書式)。

---

# Phase B: Cloud structure extraction

Tableau Server/Cloud 上の **publish 先プロジェクト階層** を REST API で読み取り、後段が消費できる `deploy-context.md` を生成する。**読み取り専用**、副作用なし。

## なぜこのフェーズが必要か

- decompose（分解設計）時点で **既存 flow 名との衝突** を避けたい
- prep-deployer の preflight が「サブプロジェクト不足 → 作成承認を取る」判断材料を必要とする
- publish 時に「サインインしたユーザーがそもそも書き込み権限を持たない site / project だった」という遅発の事故を防ぐ
- **URL ID (`/projects/1117306` の数値) からの LUID 解決は REST 標準では不可能** なので、`Parent/Child` path での解決ロジックをここに集約

## 入力

| 入力 | 扱い |
|---|---|
| target path（深さ自由、例: `"99_Sandbox/Q4-2026/flow241407_decompose"`） | top-level から `parent_id` チェーンを walk。途中で見つからないセグメントは pending |
| または target LUID | `server.projects.get_by_id` で直接取得、parent chain を逆走して existing prefix を再構成 |
| `.env`（Repo 直下 or ユーザー作業フォルダ） | `SERVER`, `SITE_NAME` (OAuth ブラウザサインインで認証、secret は持たない) |

加えて出力先 `deploy-context.md` のパス。

## 出力

メイン会話への戻り値の末尾に **`## Timing` ブロック** を必ず含める ([references/skill-timing-contract.md](../../../references/skill-timing-contract.md))。Phase B の breakdown 推奨項目: `project tree fetch` / `parent walk + naming scan` / `write`。

**`deploy-context.md`** 1 枚。frontmatter (target_path / target_status / target_luid / existing_prefix_path / existing_prefix_luid / pending_segments) + 7 セクションの本文。書式詳細 / 階層モデル (target = stg/int/marts の直上) / `get_project_structure.py` 実行例 / URL ID (vizportalUrlId) の LUID 逆引き不可問題 / 制約 は [references/phase-b-procedure.md](references/phase-b-procedure.md) を Read で取得。

自然言語による path 指示の path 化は **caller (メインエージェント) の責務**。本 Skill は確定済み path のみ受ける ([CLAUDE.md](../../../CLAUDE.md) Session intake Q4 補足参照)。

---

# Phase C: Input dispatch

分解元 Prep フローの各 Input ノードについて、後段 architect / builder がどう扱うかを **AI 提案 + ユーザー確認** で確定する。Phase A の flow.json と Phase B の deploy-context.md の両方が前提。

## なぜこのフェーズが必要か

- Input 種別 (構造) は `flow_io.inspect_input_node()` で決定論的に取れるが、**取扱方針 (整形済 PDS なので passthrough か / raw vconn なので augment か / direct DB なので block か)** は業務判断であり auto-detect 禁止
- direct_db Input は **Prep に認証情報を埋め込まない方針** のため本ワークフローではサポート外。ユーザーに「Cloud 側で先に仮想接続化 / PDS 化」を促す escalation 出口が必要
- 日本語 caption の snake_case 化など列名 semantic 提案は LLM 仕事 (機械翻訳不可能、業務文脈推定が必要)。fork 内で AI が提案を起こし、ユーザーが行単位で受け入れ/上書き

## 入力

| 入力 | 扱い |
|---|---|
| `flow.json` (Phase A 出力 or 同等) | Input ノード分類 + vconn metadata 抽出 + 列メタ |
| `deploy-context.md` (Phase B 出力) | PDS Input の LUID 解決に使用。target_path + **Input PDS 親プロジェクト** を `--also-scan` で含めて Phase B を回した結果を渡すのが理想 |
| 出力先 `input-dispatch.md` のパス | 典型: `work/<yyyymmdd>_<tag>/reports/input-dispatch.md` |

## 出力

メイン会話への戻り値の末尾に **`## Timing` ブロック** を必ず含める ([references/skill-timing-contract.md](../../../references/skill-timing-contract.md))。Phase C の breakdown 推奨項目: `mech classify (dispatch_inputs.py)` / `proposal compose (LLM)` / `write`。

**`input-dispatch.md`** 1 枚 (`status: pending` で書き出し、ユーザー確認後に main agent が `confirmed` に上書き)。書式は [references/input-dispatch-format.md](references/input-dispatch-format.md)、詳細手順 (mechanical script vs LLM 責務分担 / policy 提案ルール / block 時の escalation 文) は [references/phase-c-procedure.md](references/phase-c-procedure.md) を Read で取得。

## 手順

1. `scripts/dispatch_inputs.py` を実行して mechanical findings JSON を取得 (Input 分類 / PDS LUID 解決 / vconn metadata / 列メタ整理)
2. JSON を読んで Input ごとに policy 提案 (passthrough / augment / block) と policy 級 Transforms 提案を組み立て
3. 非 ASCII caption (日本語等) は semantic translation を提案 (`数量` → `quantity`)、ASCII は機械的 snake_case 化
4. block (direct_db / extract / unknown) があれば session 全体停止の escalation 文を含める
5. proposal markdown を `status: pending` で書き出してメイン会話に戻す (main agent がユーザー確認 → confirmed に書き換え)

## block 検出時の挙動

`blocks_present: true` のときは frontmatter にも本文にも明示。main agent は decompose に進まず Cloud 側整備 (vconn 化 / PDS 化) をユーザーに依頼する。再開時は Phase A から (flow 自体が変わるため)。

---

## 後段への引き渡し

| 後段 Skill | 渡すファイル |
|---|---|
| prep-architect (analyze / decompose) | `flow-summary.md` + `deploy-context.md` + `input-dispatch.md` (confirmed) |
| prep-builder | `decomposition-plan.md`（prep-architect 出力） |
| prep-deployer (preflight) | `deploy-context.md` |
| prep-deployer (publish) | `flows/**/*.tfl` + `flows/staging/*.augmenter.json` + `deploy-context.md` |

後段 Skill は flow.json や REST API を **直接叩かず**、本 Skill の出力 markdown のみを読む。
