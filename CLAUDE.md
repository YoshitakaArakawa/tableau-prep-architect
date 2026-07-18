# tableau-prep-architect

## Overview

このリポジトリは Tableau Prep の長大化したフロー (.tfl/.tflx) を、dbt 流のレイヤ規律（staging / intermediate / marts）で **分析・分解設計・再構築** するための Claude Code エージェント環境。個々の Skill 単体ではなく、**移行のオーケストレーション (migration-workflow の手順 + 起動規則 + work/ 規律 + 共通 scripts/references + Codex 入口) を一体にした作業環境**として、リポごと clone して中で移行セッションを回すことを想定する (中身を参考にするだけでも可)。**dbt 自体は使わない**——コンセプトのみ転用。詳細な思想・利用条件・スコープ外（push-down 提案など）は [README.md](README.md#設計思想--使いどころ) 参照。

## 起動規則 (最重要)

ユーザーが既存 Prep フローの **分析 / 分解設計 / 移行 / Cloud publish / E2E 比較 / スケジュール設計 / Workbook repoint / Pulse repoint / backfill** を依頼したら、他の作業に入る前に **必ず [references/migration-workflow.md](references/migration-workflow.md) を読み**、その step 0 (Session intake) から実行する。移行タスクの実行手順 (workflow 表・intake Q1-Q5・Stop 1/2・deploy-context ライフサイクル・goal ゲート・targeted fix ループ) は migration-workflow.md が正典で、CLAUDE.md には持たない。

## Skill 構成

| Skill | 役割 | 副作用 |
|---|---|---|
| [tableau-prep-extractor](.claude/skills/tableau-prep-extractor/SKILL.md) | Phase A flow→flow-summary / Phase B Cloud 階層+Input 分類+PDS LUID→deploy-context / Phase C flow 依存マップ (複数フロー時) (fork) | ローカル / Cloud 読み取りのみ |
| [tableau-prep-architect](.claude/skills/tableau-prep-architect/SKILL.md) | analyze (業務解釈・レイヤ推定) + decompose (分解設計、Stop 2 でユーザー確認) (fork) | ローカル |
| [tableau-prep-builder](.claude/skills/tableau-prep-builder/SKILL.md) | 設計案から .tfl 群を組み立て (fork で元 .tfl JSON を隔離) | ローカル |
| [tableau-prep-deployer](.claude/skills/tableau-prep-deployer/SKILL.md) | preflight / publish / run。合意のみで一気通貫、失敗は autonomous-recovery で自律ループ | サーバー書込 |
| [tableau-pds-comparator](.claude/skills/tableau-pds-comparator/SKILL.md) | 元 PDS vs 分解後 PDS の列差分 + 全体行数差分を Markdown 出力 (fork) | ローカル / Cloud 読み取りのみ |
| [tableau-pds-augmenter](.claude/skills/tableau-pds-augmenter/SKILL.md) | PDS への calc 注入 + column transforms (rename/cast/hide)。stg を Live PDS で表現する経路 | サーバー書込 (PDS publish) |
| [tableau-prep-schedule-designer](.claude/skills/tableau-prep-schedule-designer/SKILL.md) | design (Linked Task 設計資料) / verify (UI 作成後にサーバー実測突合) (fork) | ローカル / Cloud 読み取りのみ |
| [tableau-workbook-repointer](.claude/skills/tableau-workbook-repointer/SKILL.md) | design (旧 PDS 参照 WB 棚卸し + 旧→新 対応) / repoint (TWB 手術で自動差し替え、リハーサル→承認→本番の段取りゲート付き) / verify (lineage 突合) (fork) | サーバー書込 (WB republish、repoint モードのみ) |
| [tableau-pulse-repointer](.claude/skills/tableau-pulse-repointer/SKILL.md) | design (旧 PDS 参照 Pulse 定義 + follower 棚卸し) / repoint (コピー定義作成 + metric/購読再作成、rehearsal→承認→production の段取りゲート付き) / verify (実測突合) (fork) | サーバー書込 (Pulse 定義/購読作成、repoint モードのみ) |
| [tableau-pds-backfiller](.claude/skills/tableau-pds-backfiller/SKILL.md) | incremental accumulator に旧 output PDS 履歴を seed。段取りゲート付き | サーバー書込 (本番 PDS Overwrite) |
| [tableau-prep-migration-planner](.claude/skills/tableau-prep-migration-planner/SKILL.md) | 複数フロー/横断工程の scope・移行順・人間作業・進捗を migration-plan に集約 (fork なし) | ローカル |

役割対称性: 読み取り = tableau-prep-extractor + tableau-pds-comparator + tableau-prep-schedule-designer / 書き込み = tableau-prep-deployer (+ augmenter, backfiller, workbook-repointer / pulse-repointer の repoint モード) / オーケストレーション = [references/migration-workflow.md](references/migration-workflow.md) (手順) + tableau-prep-migration-planner (セッション横断台帳)。

## work/ ディレクトリ規約

このリポジトリ内で動くセッションの全成果物 (Skill 出力 / .tfl / build スクリプト) は `work/<yyyymmdd>_<tag>/` に集約する (`<tag>` は Session intake の [Q3](references/migration-workflow.md#step-0-session-intake))。「スクラッチ (使い捨ての遊び場)」ではなく公式の置き場。直下は **入力 (.tfl / flow.json) + 4 サブフォルダ (`reports/` `flows/` `scripts/` `scratch/`) で固定** — ファイルの「役割」で分離し、Skill が増えても直下を膨張させない。各サブフォルダの責務 (入れるもの / 入れないもの)・昇格ルール・実行時間の事後計測 tip は [work/README.md](work/README.md) を参照。git 追跡は `work/README.md` のみ。

移行セッションはこのリポを clone した中で回すのが既定で、全成果物は上記 `work/<yyyymmdd>_<tag>/` 配下に隔離する (リポ外に出さない)。

**このリポジトリの直下に `flows/` / `models/` 等のデータディレクトリを作らない**。データ実体は必ず `work/<yyyymmdd>_<tag>/` 配下に隔離する — リポ直下に置くと、追跡対象であるリポ本体 (Skill / scripts / references / 手順書) との混在・`.gitignore` 漏れ・肥大を招く。

## Repo 構造

ディレクトリ実体は `ls` で確認できるためここでは図にしない (drift するため)。新規 script / reference を **どこに置くか** の判断基準のみ規定:

| 場所 | 入る対象 |
|---|---|
| repo 直下 `scripts/` | **2 つ以上の Skill が import / 呼び出す** 共通モジュール (例: `tableau_auth.py`, `flow_io.py`, `publish_manifest.py`)、**main agent が migration-workflow の手順として直接実行する orchestrator** (例: `consumer_probe.py`, `run_layer.py`)、**セッション生成スクリプトが import する helper** (例: `build_helpers.py`) |
| `.claude/skills/<skill>/scripts/` | **その Skill 専用、外から呼ばれない** (例: tableau-prep-extractor の `inspect_actions.py`、tableau-prep-deployer の `publish_flow.py`) |
| repo 直下 `references/` | **2 つ以上の Skill が参照する共通規約・スキーマ・ポリシー** (例: `input-policy.md`, `naming-conventions.md`, `tfl-json-schema.md`, `project-hierarchy.md`) |
| `.claude/skills/<skill>/references/` | **その Skill 専用のレシピ・フォーマット仕様** (例: `flow-summary-format.md`, `build-recipe.md`, `preflight-recipe.md`) |

判断基準: **2 つ以上で使うなら repo 直下、単一 Skill 内で完結するなら Skill 配下**。Skill 配下のファイルを別 Skill も使いたくなったら repo 直下に **昇格** する (ファイル移動 + 参照箇所更新、転送 stub は置かない)。逆向き (repo 直下 → Skill 配下) は基本ない。例外: repo 直下の orchestrator が Skill 配下のスクリプトを subprocess で呼ぶことは許容する (例: `run_layer.py` → tableau-prep-deployer の `run_flow.py`)。

## 認証情報の運用

REST 認証は OAuth 2.0 (Authorization Code + PKCE) のブラウザサインイン。`.env` は `SERVER` / `SITE_NAME` のみ (secret を持たない、[.env.template](.env.template) 参照、実 `.env` は `.gitignore` 済)。実装は [scripts/tableau_auth.py](scripts/tableau_auth.py) (`signed_in_server()`)、`access_token` は `.auth-cache/session.json` にキャッシュ。詳細運用 (logout / status / CI 向け PAT 切り出し等) は [tableau-prep-deployer/references/authentication.md](.claude/skills/tableau-prep-deployer/references/authentication.md)。

## Codex 対応

OpenAI Codex ユーザー向けの入口を別途持つ。Skill の正典は `.claude/skills/` のままで、Codex 向けは薄い wrapper + 読み替え方式:

- [AGENTS.md](AGENTS.md) — Codex エントリポイント。この CLAUDE.md と同内容の規範 + Claude Code 固有記法 (`context: fork` / `model` / `${CLAUDE_SKILL_DIR}` 等) の読み替え表を持つ
- `.agents/skills/<name>/SKILL.md` — 11 個の thin wrapper。正典 SKILL.md へのリンクと実行モード指示のみ (`scripts/sync_agents_skills.py` で生成)
- `.codex/` — trust ゲート・MCP テンプレート・サブエージェント定義 (flow-worker / flow-worker-lite)。trusted プロジェクトでのみ有効

この CLAUDE.md (起動規則・Skill 構成・work/ 規約・repo 構造・認証) や Skill の `description` を変更したら、AGENTS.md と wrapper の同期を取り、`python scripts/sync_agents_skills.py --check` (exit 0 = 同期済み) で検証する。
