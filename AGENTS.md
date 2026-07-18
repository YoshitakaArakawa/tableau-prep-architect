# AGENTS.md — Codex 向けエントリポイント

このファイルは OpenAI Codex ユーザーがこのリポジトリの Skill 集を使うための入口です。Skill の**正典は `.claude/skills/` のまま**で、Codex 向けには本ファイルの「読み替え表」と `.agents/skills/` の薄い wrapper を通じてアクセスします。Claude Code 利用者向けの規範は [CLAUDE.md](CLAUDE.md) が持ち、本ファイルはその Codex 版として**同内容の規範**を提供します。

## リポ概要

このリポジトリは Tableau Prep の長大化したフロー (.tfl/.tflx) を、dbt 流のレイヤ規律 (staging / intermediate / marts) で **分析・分解設計・再構築** するための Skill 集です。**dbt 自体は使いません**——コンセプトのみ転用します。設計思想・利用条件・スコープ外 (push-down 提案など) の詳細は [README.md](README.md#設計思想--使いどころ) を参照してください。

## 起動規則 (最重要)

ユーザーが既存 Prep フローの **分析 / 分解設計 / 移行 / Cloud publish / E2E 比較 / スケジュール設計 / Workbook repoint / backfill** を依頼したら、他の作業に入る前に **必ず [prep-migrate](.claude/skills/prep-migrate/SKILL.md) の SKILL.md を読み**、その Workflow / Session intake 手順に従ってください。移行タスクの実行手順 (workflow 図・intake Q1-Q5・Stop 1/2・deploy-context ライフサイクル・goal ゲート・targeted fix ループ・courier 責務) は prep-migrate が正典で、本ファイルには持ちません。

`$prep-migrate` のように明示起動しても、自然言語依頼で暗黙起動しても構いません。いずれの経路でも、実体の手順は正典 SKILL.md を読んで実行します。

## Skill 一覧

各 Skill の正典は `.claude/skills/<name>/SKILL.md` です。「実行モード」列は、その Skill を **主会話で実行するか / サブエージェントに委譲するか** を示します (根拠は後述の「Claude Code 記法の読み替え表」と「fork の意味論」)。

| Skill | 役割 | 正典パス | 実行モード |
|---|---|---|---|
| prep-migrate | 移行セッションの entry-point 手順書 (intake + workflow + Stop 運用) | [.claude/skills/prep-migrate/SKILL.md](.claude/skills/prep-migrate/SKILL.md) | 主会話 |
| prep-extractor | Phase A flow→flow-summary / Phase B Cloud 階層+Input 分類+PDS LUID→deploy-context | [.claude/skills/prep-extractor/SKILL.md](.claude/skills/prep-extractor/SKILL.md) | サブエージェント委譲 (flow-worker-lite) |
| prep-architect | analyze (業務解釈・レイヤ推定) + decompose (分解設計、Stop 2 でユーザー確認) | [.claude/skills/prep-architect/SKILL.md](.claude/skills/prep-architect/SKILL.md) | サブエージェント委譲 (flow-worker) |
| prep-builder | 設計案から .tfl 群を組み立て (元 .tfl JSON を隔離) | [.claude/skills/prep-builder/SKILL.md](.claude/skills/prep-builder/SKILL.md) | サブエージェント委譲 (flow-worker) |
| prep-deployer | preflight / publish / run。合意のみで一気通貫、失敗は autonomous-recovery で自律ループ | [.claude/skills/prep-deployer/SKILL.md](.claude/skills/prep-deployer/SKILL.md) | 主会話 |
| prep-output-comparator | 元 PDS vs 分解後 PDS の列差分 + 全体行数差分を Markdown 出力 | [.claude/skills/prep-output-comparator/SKILL.md](.claude/skills/prep-output-comparator/SKILL.md) | サブエージェント委譲 (flow-worker) |
| prep-pds-augmenter | PDS への calc 注入 + column transforms (rename/cast/hide) | [.claude/skills/prep-pds-augmenter/SKILL.md](.claude/skills/prep-pds-augmenter/SKILL.md) | 主会話 |
| prep-schedule-designer | design (Linked Task 設計資料) / verify (UI 作成後にサーバー実測突合) | [.claude/skills/prep-schedule-designer/SKILL.md](.claude/skills/prep-schedule-designer/SKILL.md) | サブエージェント委譲 (flow-worker) |
| prep-workbook-repointer | design (旧 PDS 参照 WB 棚卸し + 旧→新 対応) / repoint (TWB 手術で自動差し替え、リハーサル→承認→本番の段取りゲート付き) / verify (lineage 突合) | [.claude/skills/prep-workbook-repointer/SKILL.md](.claude/skills/prep-workbook-repointer/SKILL.md) | サブエージェント委譲 (flow-worker) |
| prep-pds-backfiller | incremental accumulator に旧 output PDS 履歴を seed。段取りゲート付き | [.claude/skills/prep-pds-backfiller/SKILL.md](.claude/skills/prep-pds-backfiller/SKILL.md) | 主会話 |
| prep-migration-planner | 複数フロー/横断工程の scope・移行順・人間作業・進捗を migration-plan に集約 | [.claude/skills/prep-migration-planner/SKILL.md](.claude/skills/prep-migration-planner/SKILL.md) | 主会話 |

役割対称性: 読み取り = prep-extractor + prep-output-comparator + prep-schedule-designer / 書き込み = prep-deployer (+ augmenter, backfiller, workbook-repointer の repoint モード) / オーケストレーション = prep-migrate (手順) + prep-migration-planner (セッション横断台帳)。

Codex 向けの入口は `.agents/skills/<name>/SKILL.md` (11 個の薄い wrapper) です。wrapper は正典 SKILL.md へのリンクと実行モードの指示だけを持ち、実体は上表の正典パスを読んで実行します。

## Claude Code 記法の読み替え表

正典 SKILL.md には Claude Code 固有の frontmatter フィールドや変数記法が含まれます。Codex にはこれらに相当する機構が無いものがあるため、次の表に従って解釈してください。**このファイルが読み替えの正典**です。

| Claude Code 記法 | Codex での解釈 |
|---|---|
| frontmatter `context: fork` | サブエージェントに委譲して実行する (下記「fork の意味論」参照)。委譲できない環境ではインライン実行してよいが、出力契約は必ず維持する |
| frontmatter `agent: general-purpose` | 既定のサブエージェント種別で可。特別な指定は不要 |
| frontmatter `model: haiku` | 軽量・機械的タスク。低 reasoning effort で実行する (`.codex/agents/flow-worker-lite.toml`)。対象は prep-extractor のみ |
| frontmatter `model: sonnet` / 無指定 | 標準の reasoning effort で実行する (`.codex/agents/flow-worker.toml`) |
| frontmatter `allowed-tools` | 無視する。Codex の approval / sandbox 設定に従う (ツール制限は Codex 側の権限モデルが担う) |
| `${CLAUDE_SKILL_DIR}` | その SKILL.md が置かれているディレクトリ (= `.claude/skills/<name>`) に読み替える。相対パスの基準点として使う |
| `CLAUDE.md` への参照リンク | そのまま読む。命名規約・配置規約・work/ 規約はエージェント共通で、Codex でも同じ規範に従う |
| `references/fork-skill-contract.md` / `## Timing` ブロック | Codex でもそのまま従う (下記「fork の意味論」の (b)(c))。委譲の有無にかかわらず出力契約は不変 |

### fork の意味論

正典で `context: fork` が付く 6 Skill (prep-extractor / prep-architect / prep-builder / prep-output-comparator / prep-schedule-designer / prep-workbook-repointer) の fork には 3 つの意義があります:

- (a) **メイン会話のコンテキスト保護** — 大きな JSON / 中間生成物を主会話に流さない
- (b) **会話履歴なし前提の入力明示契約** — 呼び出し時に必要情報を文章ですべて渡す (fork 側は「会話に出ていたはず」を前提にしない)
- (c) **成果物はファイル出力、返答は要約 + `## Timing` ブロックのみ** — 正典は [references/fork-skill-contract.md](references/fork-skill-contract.md) と [references/skill-timing-contract.md](references/skill-timing-contract.md)

**Codex への写像**: fork 系 Skill は、`.codex/agents/` のサブエージェント (flow-worker / flow-worker-lite) に委譲して実行します。サブエージェント機能が使えない場合は組み込みのサブエージェント、それも無ければインライン実行にフォールバックして構いません。**ただしどの経路でも (b) 入力明示契約と (c) 出力契約 (ファイル出力・主会話へ中間 JSON を流さない・返答は要約 + Timing ブロック) は必ず維持します**。インライン実行はコンテキスト隔離 (a) の保証が弱まるだけで、契約自体は免除されません。

fork しない 5 Skill (prep-migrate / prep-migration-planner / prep-deployer / prep-pds-augmenter / prep-pds-backfiller) は、**ユーザー承認ゲート・失敗観測を主会話で扱う**ための意図的設計です。これらはサブエージェントに委譲せず、主会話で実行してください。

## work/ ディレクトリ規約

このリポジトリ内で動くセッションの全成果物 (Skill 出力 / .tfl / build スクリプト) は `work/<yyyymmdd>_<tag>/` に集約します (`<tag>` は Session intake の Q3)。直下は **入力 (.tfl / flow.json) + 4 サブフォルダ (`reports/` `flows/` `scripts/` `scratch/`)** で固定し、ファイルの「役割」で分離します。各サブフォルダの責務・昇格ルールは [work/README.md](work/README.md)、規約全体は [CLAUDE.md](CLAUDE.md#work-ディレクトリ規約) を参照してください。git 追跡は `work/README.md` のみです。

判定境界: 作業場所がこのリポジトリの内側なら `work/` 配下、外側 (ユーザー自身の Prep プロジェクト) ならそのプロジェクト直下です。**このリポジトリの直下に `flows/` / `models/` 等のデータディレクトリは作りません** (Skill 配布専用のため)。

## repo 構造・認証

新規 script / reference の配置基準は **「2 つ以上の Skill が使うなら repo 直下、単一 Skill 内で完結するなら Skill 配下」** です:

- repo 直下 `scripts/` / `references/` — 2 つ以上の Skill が共有する共通モジュール・規約 (例: `tableau_auth.py`, `input-policy.md`)
- `.claude/skills/<skill>/scripts/` / `references/` — その Skill 専用で外から呼ばれないもの

詳細は [CLAUDE.md](CLAUDE.md#repo-構造) を参照してください。

認証は OAuth 2.0 (Authorization Code + PKCE) のブラウザサインインです。`.env` は `SERVER` / `SITE_NAME` のみを持ち、secret は持ちません ([.env.template](.env.template) 参照、実 `.env` は `.gitignore` 済)。実装は [scripts/tableau_auth.py](scripts/tableau_auth.py) の `signed_in_server()`、`access_token` は `.auth-cache/session.json` にキャッシュされます。この `.env` は Claude Code と Codex で共通です。

## Codex セットアップ

Codex での有効化手順 (リポを trusted にする / Tableau MCP を `.codex/config.toml` で設定する / `.env` 共通運用) は README の「Codex で使う」節を参照してください。`.codex/` の内容と trust ゲートの詳細は [.codex/README.md](.codex/README.md) にあります。`.codex/` は trusted プロジェクトでのみ有効で、untrusted では黙って無視されます (その場合はインライン実行にフォールバックします)。

## メンテ注記

- [CLAUDE.md](CLAUDE.md) と本ファイルは**同内容の規範**を持ちます。CLAUDE.md 側 (起動規則・Skill 構成・work/ 規約・repo 構造・認証) を変更したら、本ファイルの対応箇所も更新してください。
- Skill の `description` や fork/model 分類を変更したら、`.agents/skills/` の wrapper が正典と drift します。`python scripts/sync_agents_skills.py --check` を実行して同期を検証してください (exit 0 が同期済み、exit 1 なら wrapper 再生成が必要)。
- `.codex/agents/` のサブエージェント名 (flow-worker / flow-worker-lite) は、上表・読み替え表・wrapper と一致している必要があります。エージェント名を変えるときは 3 箇所すべてを更新してください。
