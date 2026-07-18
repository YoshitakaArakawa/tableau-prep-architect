---
name: prep-extractor
description: Tableau Prep の .tfl / .tflx / flow.json およびサーバー上のプロジェクト階層を読み、後段が直接 JSON / REST を見なくて済むコンパクトなサマリに再構成する Skill。Phase A = flow extraction（flow-summary.md）、Phase B = cloud context extraction（deploy-context.md + input-dispatch-mech.json）、Phase C = flow dependency mapping（flow-dependencies.md、複数フロー移行の計画時のみ）の 3 フェーズを持つ。大きな Prep フロー（数十〜数百ノード）を解析・分解する前、または Tableau Cloud に publish する前に必ず実行する。ユーザーが「フローを extract して」「flow-summary を作って」「publish 先のプロジェクトを確認して」「Input を分類して」「フロー間の依存を調べて」「移行順を決めて」と言ったとき、サーバー上のフローを DL したいときに起動（list_flows.py / download_flow.py）。移行セッション冒頭の intake・goal ゲート・起動順序は references/migration-workflow.md が正典（本 Skill 単体で移行セッションを始めない）。
context: fork
model: haiku
allowed-tools: Read Write Bash(python *) Bash(mkdir *) Glob Grep
---

# prep-extractor

Tableau Prep のフロー定義 JSON および Tableau Server/Cloud のプロジェクト階層を読み、後段の Skill が **flow.json や REST API を直接叩かなくて済む** コンパクトな markdown / JSON サマリを生成する Skill。**読み取り専用**（書き込み副作用は無い）。

## フェーズ

| フェーズ | 入力 | 出力 | スクリプト |
|---|---|---|---|
| **A: Flow extraction** | `.tfl` / `.tflx` / `flow.json` | `flow-summary.md` | `gen_flow_summary.py` (5 セクション一括生成) |
| **B: Cloud context extraction** | target path / LUID + `flow.json` (Phase A 出力) | `deploy-context.md` + `input-dispatch-mech.json` | `get_project_structure.py` + `dispatch_inputs.py` |
| **C: Flow dependency mapping** (optional) | フロー群 (ローカル files / dir、または `--project` でサーバー一括 DL) | `flow-dependencies.md` | `map_flow_dependencies.py` |

Phase A は独立に呼べる。Phase B は内部で `get_project_structure.py` と `dispatch_inputs.py` を順次回し、target_path walk + Input PDS 親プロジェクトの自動 `--also-scan` 解決 + Input ノード kind 分類 + PDS LUID 解決を 1 フェーズで完結させる。decompose 時点で `flow-summary.md` + `deploy-context.md` + `input-dispatch-mech.json` の 3 ファイルが揃っているのが理想。

Phase C は **複数フロー移行の計画時に 1 回** 実行するプロジェクトスコープの補助フェーズ（毎セッションでは走らせない — 依存はプロジェクト内で安定、フロー集合が変わった時のみ再生成）。詳細は「[Phase C](#phase-c-flow-dependency-mapping-optional)」節。

## Caller から渡される入力

`context: fork` で動くため caller (メインエージェント) は会話履歴を渡せない。起動時に以下を文章で明示すること:

| 入力 | 必須 | 例 |
|---|---|---|
| `phase` | ✅ | `A` (flow extraction) / `B` (cloud context) / `C` (dependency mapping) / `all` (A+B) |
| `input_path` | Phase A で必須 | ローカル `.tfl/.tflx/flow.json` のパス。サーバー DL から始める場合は flow 名 / LUID |
| `target_path` | Phase B で必須 | publish 先 target のフルパス。LUID 指定時は `target_luid` |
| `flow_json_path` | Phase B で必須 | Phase A が展開済の `flow.json` のパス (Input 分類 + PDS 親プロジェクト集合の自動算出に使用) |
| `flow_set` | Phase C で必須 | フロー群 (ローカル files / dir) またはサーバープロジェクト名 (例: `1_Prep`) |
| `output_path` | ✅ | `flow-summary.md` (Phase A) / `deploy-context.md` + `input-dispatch-mech.json` (Phase B) / `flow-dependencies.md` (Phase C) の出力先 (典型: `work/<yyyymmdd>_<tag>/reports/`)。MD/JSON レポートは [CLAUDE.md §work/ ディレクトリ規約](../../../CLAUDE.md#work-ディレクトリ規約) の `reports/` に集約 |

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
4. **SuperTransform actions inventory** — 各 Clean ステップの操作を 1 行サマリ化。flat SuperTransform / Container / Input renames の 3 形式を収録 (形式タグ付き)
5. **Warnings** — 未知 nodeType / action type、空ノード、同名ノード、孤立ノード等

メイン会話への戻り値の末尾に **`## Timing` ブロック** を必ず含める (フォーマットと Phase 別 breakdown 推奨項目: [skill-timing-contract.md](../../../references/skill-timing-contract.md))。

## 手順

4 ステップ (入力展開 → `gen_flow_summary.py` 実行で 5 セクション一括生成 → 生成結果レビュー → 完了報告) で構成。セクションの手組みはしない。詳細手順・エラーハンドリング・サーバー DL の補助コマンド・制約は [references/flow-extraction-procedure.md](references/flow-extraction-procedure.md) を Read で取得。

主要参照: [tfl-json-schema.md](../../../references/tfl-json-schema.md) (JSON 構造 + UI⇔nodeType / actions マッピング) / [references/flow-summary-format.md](references/flow-summary-format.md) (出力書式)。

---

# Phase B: Cloud context extraction

Tableau Server/Cloud 上の **publish 先プロジェクト階層** を REST API で読み取り、**Prep flow の Input ノード分類 + PDS LUID 解決** も併せて行うフェーズ。**読み取り専用**、副作用なし。**ユーザー確認は持たない** (policy 提案 / stg rename / provisioning 確認は architect Stop 2 に集約)。

## なぜこのフェーズが必要か

- decompose（分解設計）時点で **既存 flow 名との衝突** を避けたい
- prep-deployer の preflight が「サブプロジェクト不足 → 作成承認を取る」判断材料を必要とする
- publish 時に「サインインしたユーザーがそもそも書き込み権限を持たない site / project だった」という遅発の事故を防ぐ
- **URL ID (`/projects/1117306` の数値) からの LUID 解決は REST 標準では不可能** なので、`Parent/Child` path での解決ロジックをここに集約
- Input ノード分類 (`flow_io.inspect_input_node`) と PDS LUID 解決 (deploy-context.md scan) は同じ「Cloud 状態スナップショット」の一部で、両方とも deploy-context.md に依存する mechanical 処理。architect (解釈 Skill) ではなく extractor (読み取り Skill) に置く方が責務対称
- 「整形済 PDS なので passthrough」「raw vconn なので augment」といった **policy 判断は業務知識依存** で本 Skill の責務外。architect に集約

## 実行と出力

入力は target path (または target LUID) + `flow.json` (Phase A 出力) + `.env` (`SERVER` / `SITE_NAME`)。出力は **2 ファイル**:

- `deploy-context.md` — Cloud project hierarchy (frontmatter + 8 セクション)
- `input-dispatch-mech.json` — 各 Input ノードの kind 分類 + LUID 解決 + vconn metadata + fields[]。書式は [references/input-dispatch-format.md](references/input-dispatch-format.md)

内部は 3 ステップ (1-pass target_path scan → Input dispatch + 親プロジェクト集合の抽出 → 必要なら `--also-scan` 再 scan + dispatch 再実行)。**手順の CLI・入力の扱い・エラーハンドリング・unknown 検出時の挙動 (exit 2 = session 中断 / direct_db・extract は中断せず provisioning 経路) ・URL ID の LUID 逆引き不可問題は [references/deploy-context-procedure.md](references/deploy-context-procedure.md) を Read で取得** (実行前に必読)。

**preflight 後の再実行 (migration-workflow step 4)**: goal ≥ ④ (Cloud publish) では初回 Phase B の時点で stg/int/marts プロジェクトが未作成 (presence=no) なので layer LUID が空。prep-deployer preflight が 3 レイヤを作成した後に **Phase B をもう一度実行** して `deploy-context.md` の layer 行に LUID を埋め、その更新版を入力に decompose の `gen_plan_skeleton.py` が plan.json の `flow_projects` / `ds_projects` を充填する。goal ②/③ (ローカル完結) では preflight も Phase B 再実行も走らせず、plan.json の layer LUID は TODO placeholder のまま許容する。

メイン会話への戻り値の末尾に **`## Timing` ブロック** を必ず含める ([skill-timing-contract.md](../../../references/skill-timing-contract.md))。

---

# Phase C: Flow dependency mapping (optional)

複数フローをまとめて移行する計画時に、**フロー間の依存 (A の入力 PDS = B の出力 PDS) を機械抽出して着手順を確定する** フェーズ。読み取り専用。フロー名やドメイン直感から着手順を推定すると外れるため、必ず本フェーズの出力で確定させる。**毎セッションでは走らせない** (再生成条件は [references/dependency-mapping.md](references/dependency-mapping.md))。

実行 CLI (`map_flow_dependencies.py`)・出力 `flow-dependencies.md` のセクション構成と消費者は [references/dependency-mapping.md](references/dependency-mapping.md) を Read で取得。

---

## 後段への引き渡し

| 後段 Skill | 渡すファイル |
|---|---|
| prep-architect (analyze / decompose) | `flow-summary.md` + `deploy-context.md` + `input-dispatch-mech.json` (+ 複数フロー移行時は `flow-dependencies.md`) |
| prep-builder | `decomposition-plan-<flow>.json`（prep-architect 出力、設計の正） |
| prep-deployer (preflight) | `deploy-context.md` |
| prep-deployer (publish) | `flows/**/*.tfl` + `flows/staging/*.augmenter.json` + `deploy-context.md` |

後段 Skill は flow.json や REST API を **直接叩かず**、本 Skill の出力 markdown / JSON のみを読む。
