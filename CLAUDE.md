# tableau-prep-architect

## Overview

このリポジトリは Tableau Prep の長大化したフロー (.tfl/.tflx) を、dbt 流のレイヤ規律（staging / intermediate / marts）で **分析・分解設計・再構築** するための Claude Code エージェント環境。個々の Skill 単体ではなく、**移行のオーケストレーション (prep-migrate の手順 + 起動規則 + work/ 規律 + 共通 scripts/references + Codex 入口) を一体にした作業環境**として、リポごと clone して中で移行セッションを回すことを想定する (中身を参考にするだけでも可)。**dbt 自体は使わない**——コンセプトのみ転用。詳細な思想・利用条件・スコープ外（push-down 提案など）は [README.md](README.md#設計思想--使いどころ) 参照。

## 起動規則 (最重要)

ユーザーが既存 Prep フローの **分析 / 分解設計 / 移行 / Cloud publish / E2E 比較 / スケジュール設計 / Workbook repoint / Pulse repoint / backfill** を依頼したら、他の作業に入る前に **必ず [prep-migrate](.claude/skills/prep-migrate/SKILL.md) skill を起動**し、その Workflow / Session intake 手順に従う。移行タスクの実行手順 (workflow 図・intake Q1-Q5・Stop 1/2・deploy-context ライフサイクル・goal ゲート・targeted fix ループ・courier 責務) は prep-migrate が正典で、CLAUDE.md には持たない。

## Skill 構成

| Skill | 役割 | 副作用 |
|---|---|---|
| [prep-migrate](.claude/skills/prep-migrate/SKILL.md) | 移行セッションの entry-point 手順書 (intake + workflow + Stop 運用、main agent 向け・fork なし) | なし (手順のみ) |
| [prep-extractor](.claude/skills/prep-extractor/SKILL.md) | Phase A flow→flow-summary / Phase B Cloud 階層+Input 分類+PDS LUID→deploy-context (fork) | ローカル / Cloud 読み取りのみ |
| [prep-architect](.claude/skills/prep-architect/SKILL.md) | analyze (業務解釈・レイヤ推定) + decompose (分解設計、Stop 2 でユーザー確認) (fork) | ローカル |
| [prep-builder](.claude/skills/prep-builder/SKILL.md) | 設計案から .tfl 群を組み立て (fork で元 .tfl JSON を隔離) | ローカル |
| [prep-deployer](.claude/skills/prep-deployer/SKILL.md) | preflight / publish / run。合意のみで一気通貫、失敗は autonomous-recovery で自律ループ | サーバー書込 |
| [prep-output-comparator](.claude/skills/prep-output-comparator/SKILL.md) | 元 PDS vs 分解後 PDS の列差分 + 全体行数差分を Markdown 出力 (fork) | ローカル / Cloud 読み取りのみ |
| [prep-pds-augmenter](.claude/skills/prep-pds-augmenter/SKILL.md) | PDS への calc 注入 + column transforms (rename/cast/hide)。stg を Live PDS で表現する経路 | サーバー書込 (PDS publish) |
| [prep-schedule-designer](.claude/skills/prep-schedule-designer/SKILL.md) | design (Linked Task 設計資料) / verify (UI 作成後にサーバー実測突合) (fork) | ローカル / Cloud 読み取りのみ |
| [prep-workbook-repointer](.claude/skills/prep-workbook-repointer/SKILL.md) | design (旧 PDS 参照 WB 棚卸し + 旧→新 対応) / repoint (TWB 手術で自動差し替え、リハーサル→承認→本番の段取りゲート付き) / verify (lineage 突合) (fork) | サーバー書込 (WB republish、repoint モードのみ) |
| [prep-pulse-repointer](.claude/skills/prep-pulse-repointer/SKILL.md) | design (旧 PDS 参照 Pulse 定義 + follower 棚卸し) / repoint (コピー定義作成 + metric/購読再作成、rehearsal→承認→production の段取りゲート付き) / verify (実測突合) (fork) | サーバー書込 (Pulse 定義/購読作成、repoint モードのみ) |
| [prep-pds-backfiller](.claude/skills/prep-pds-backfiller/SKILL.md) | incremental accumulator に旧 output PDS 履歴を seed。段取りゲート付き | サーバー書込 (本番 PDS Overwrite) |
| [prep-migration-planner](.claude/skills/prep-migration-planner/SKILL.md) | 複数フロー/横断工程の scope・移行順・人間作業・進捗を migration-plan に集約 (fork なし) | ローカル |

役割対称性: 読み取り = prep-extractor + prep-output-comparator + prep-schedule-designer / 書き込み = prep-deployer (+ augmenter, backfiller, workbook-repointer / pulse-repointer の repoint モード) / オーケストレーション = prep-migrate (手順) + prep-migration-planner (セッション横断台帳)。

## work/ ディレクトリ規約

このリポジトリ内で動くセッションの全成果物 (Skill 出力 / .tfl / build スクリプト) は `work/<yyyymmdd>_<tag>/` に集約する (`<tag>` は Session intake の [Q3](.claude/skills/prep-migrate/SKILL.md#session-intake-step-0))。「スクラッチ (使い捨ての遊び場)」ではなく公式の置き場。直下は **入力 (.tfl / flow.json) + 4 サブフォルダ (`reports/` `flows/` `scripts/` `scratch/`) で固定** — ファイルの「役割」で分離し、Skill が増えても直下を膨張させない。各サブフォルダの責務 (入れるもの / 入れないもの)・昇格ルール・実行時間の事後計測 tip は [work/README.md](work/README.md) を参照。git 追跡は `work/README.md` のみ。

移行セッションはこのリポを clone した中で回すのが既定で、全成果物は上記 `work/<yyyymmdd>_<tag>/` 配下に隔離する (リポ外に出さない)。

**このリポジトリの直下に `flows/` / `models/` 等のデータディレクトリを作らない**。データ実体は必ず `work/<yyyymmdd>_<tag>/` 配下に隔離する — リポ直下に置くと、追跡対象であるリポ本体 (Skill / scripts / references / 手順書) との混在・`.gitignore` 漏れ・肥大を招く。

## Repo 構造

ディレクトリ実体は `ls` で確認できるためここでは図にしない (drift するため)。新規 script / reference を **どこに置くか** の判断基準のみ規定:

| 場所 | 入る対象 |
|---|---|
| repo 直下 `scripts/` | **2 つ以上の Skill が import / 呼び出す** 共通モジュールまたは orchestrator (例: `tableau_auth.py`, `flow_io.py`, `publish_manifest.py`, `run_layer.py`) |
| `.claude/skills/<skill>/scripts/` | **その Skill 専用、外から呼ばれない** (例: prep-extractor の `inspect_actions.py`、prep-deployer の `publish_flow.py`) |
| repo 直下 `references/` | **2 つ以上の Skill が参照する共通規約・スキーマ・ポリシー** (例: `input-policy.md`, `naming-conventions.md`, `tfl-json-schema.md`, `project-hierarchy.md`) |
| `.claude/skills/<skill>/references/` | **その Skill 専用のレシピ・フォーマット仕様** (例: `flow-summary-format.md`, `build-recipe.md`, `preflight-recipe.md`) |

判断基準: **2 つ以上で使うなら repo 直下、単一 Skill 内で完結するなら Skill 配下**。Skill 配下のファイルを別 Skill も使いたくなったら repo 直下に **昇格** する (ファイル移動 + 参照箇所更新、転送 stub は置かない)。逆向き (repo 直下 → Skill 配下) は基本ない。

## 認証情報の運用

REST 認証は OAuth 2.0 (Authorization Code + PKCE) のブラウザサインイン。`.env` は `SERVER` / `SITE_NAME` のみ (secret を持たない、[.env.template](.env.template) 参照、実 `.env` は `.gitignore` 済)。実装は [scripts/tableau_auth.py](scripts/tableau_auth.py) (`signed_in_server()`)、`access_token` は `.auth-cache/session.json` にキャッシュ。詳細運用 (logout / status / CI 向け PAT 切り出し等) は [prep-deployer/references/authentication.md](.claude/skills/prep-deployer/references/authentication.md)。

## Codex 対応

OpenAI Codex ユーザー向けの入口を別途持つ。Skill の正典は `.claude/skills/` のままで、Codex 向けは薄い wrapper + 読み替え方式:

- [AGENTS.md](AGENTS.md) — Codex エントリポイント。この CLAUDE.md と同内容の規範 + Claude Code 固有記法 (`context: fork` / `model` / `${CLAUDE_SKILL_DIR}` 等) の読み替え表を持つ
- `.agents/skills/<name>/SKILL.md` — 12 個の thin wrapper。正典 SKILL.md へのリンクと実行モード指示のみ (`scripts/sync_agents_skills.py` で生成)
- `.codex/` — trust ゲート・MCP テンプレート・サブエージェント定義 (flow-worker / flow-worker-lite)。trusted プロジェクトでのみ有効

この CLAUDE.md (起動規則・Skill 構成・work/ 規約・repo 構造・認証) や Skill の `description` を変更したら、AGENTS.md と wrapper の同期を取り、`python scripts/sync_agents_skills.py --check` (exit 0 = 同期済み) で検証する。
