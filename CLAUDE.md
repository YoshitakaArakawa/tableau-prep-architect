# tableau-prep-architect

## Overview

このリポジトリは Tableau Prep の長大化したフロー (.tfl/.tflx) を、dbt 流のレイヤ規律（staging / intermediate / marts）で **分析・分解設計・再構築** するための Claude Code Skill 集。**dbt 自体は使わない**——コンセプトのみ転用。

詳細な思想・利用条件・スコープ外（push-down 提案など）は [README.md](README.md#設計思想--使いどころ) 参照。

## Workflow

ユーザーが既存 Prep フローを指して「分析して」「分解設計して」「dbt 風に整理して」「Tableau Cloud に publish して」「実行して」「E2E 比較して」と指示したら、各 Skill を **順次または個別に** 実行する。**Session intake (step 0) で goal / target path を確定したら、その先は extract → analyze → decompose → build → preflight → publish → run → compare まで段階間の承認を取らず一気通貫で進める**。失敗時は AI が原因を機械判定し、回復可能な種別 (例: 280003 → re-build / 409 → Overwrite / 上流 PDS 不在 → 上流 republish) は自律ループでリトライ、回復不能な種別 (認証 / 権限 / 容量 / Cloud 障害 / loop 検知発火) は escalation。compare で gap が出たらメインエージェントが Markdown + JSON を読み、prep-builder / prep-deployer の再呼び出しを判断する。詳細は [autonomous-execution-policy](.claude/skills/prep-deployer/references/autonomous-execution-policy.md) と [autonomous-recovery](.claude/skills/prep-deployer/references/autonomous-recovery.md)。

```
[step 0]  Session intake (会話)                    Q1-Q4 を 1 ターンで聞く (§Session intake 参照)
                ↓

[step 0a] prep-extractor ─ get-project-structure   target path を walk、existing prefix / pending segments に分割
                                                   → deploy-context.md（読み取りのみ）
                ↓ (deploy-context.md は decompose / preflight / publish で消費)

[step 0b] prep-deployer ─ preflight                pending segments を idempotent に作成 + target 配下に stg/int/marts

prep-extractor ─ flow-extract   .tfl/.tflx → flow-summary.md（構造抽出）
        ↓
prep-architect ─ analyze        現状把握 → analysis-<flow>.md
prep-architect ─ decompose      分解設計 → decomposition-plan-<flow>.md（deploy-context があれば名前衝突も加味）
        ↓
prep-builder ─ build            .tfl 群を生成（元 .tfl の maestroMetadata / displaySettings を同梱）
        ↓
prep-deployer ─ publish + run   レイヤ単位 (stg → int → marts) で publish → run → finishCode=0 確認
                                同レイヤ内は並列可、レイヤ間は順次
        ↓
prep-output-comparator ─ compare  元と分解後の最終 PDS を Metadata API + Tableau MCP で
                                  列差分 + 全体行数差分のみ比較 → Markdown
                                  (原因分析・修正提案・値同値性は持たない)
        ↓
prep-deployer ─ test (将来)      VDS でデータ品質テスト
```

publish 先構造のモデル (target = stg/int/marts の直上、上位は任意の深さ・命名) と path 自然言語解釈の責任分離は [prep-extractor SKILL.md](.claude/skills/prep-extractor/SKILL.md) 参照。step 0a / 0b は最初に一度走らせれば良く、その後の analyze / decompose / build を反復するときは `deploy-context.md` を再利用する。

## Session intake (step 0)

各 Skill は「必要な入力が会話に既に出ている」前提で動く。メインエージェントが Skill を呼び始める前に、必要な入力を **1 ターンでまとめてユーザーに聞いておく** (遅延収集は確認往復が増えるので避ける)。

セッション冒頭で聞く 4 項目:

| # | 質問 | 必須条件 | 受け取り後の使い道 |
|---|---|---|---|
| **Q1. 元フローの所在** | ローカル `.tfl/.tflx` パス、または Tableau Cloud 上の flow 名 / URL / LUID | 常に必須 | Phase A 入力。サーバー DL は prep-extractor の `list_flows.py` / `download_flow.py` 経由 |
| **Q2. ゴール段階** | ① 分析だけ / ② 分解設計まで / ③ .tfl 生成まで / ④ Cloud に publish & run まで / ⑤ 元フローとの E2E 比較まで | 常に必須 | ④ 以上が publish/run の合意 (以後は自律ループで進む)。⑤ は元フローも Cloud 上に存在することが前提 (元 flow LUID 必須) |
| **Q3. 作業フォルダ名** | `work/<yyyymmdd>_<タグ>/` の `<タグ>` 部分（空欄なら AI が Q1 フロー名から自動生成 → 復唱確認） | 常に必須 | そのセッションの全成果物の置き場 ([§work/ ディレクトリ規約](#work-ディレクトリ規約)) |
| **Q4. target path** | publish 先プロジェクトの path（任意深さ可、例: `99_Sandbox/flow241407_decompose`）または target LUID | Q2 が ② 以上で必須（② でも既存 flow 名衝突回避に有用） | step 0a (`get_project_structure.py --project-path`) の入力 |

補足:

- **Q4 が自然言語で来たら path に変換するのはメインエージェントの責務**。手順 (既存階層確認 → 意図復元 → 復唱合意 → 確定 path で step 0a) は prep-extractor 側の解釈レイヤではなく会話で完結
- **`.env` の確認は遅延でよい**: Q2 が ③/④/⑤ または Q1 がサーバー DL のときに必要。step 0a 実行直前に未整備なら聞く
- **復唱 (echo-back) は質問とは別**: Q3 タグ自動生成のように「AI が一度値を決めてユーザーに見せて redirect の機会を与える」のは **no-clarifying-questions モード下でも省略しない**
- URL ID 解決の詳細 (vizportalUrlId からの逆引き等) は [prep-extractor SKILL.md §URL ID 解決について](.claude/skills/prep-extractor/SKILL.md#url-id-解決について)

## Skill 構成

| Skill | 役割 | 副作用 |
|---|---|---|
| [prep-extractor](.claude/skills/prep-extractor/SKILL.md) | Phase A: flow.json → flow-summary.md / Phase B: Cloud project hierarchy → deploy-context.md（`context: fork` で大きな JSON を隔離） | ローカル（ファイル生成）、Cloud は **読み取りのみ** |
| [prep-architect](.claude/skills/prep-architect/SKILL.md) | analyze（業務解釈・レイヤ推定）+ decompose（分解設計、deploy-context があれば名前衝突も加味） | ローカル（ファイル生成） |
| [prep-builder](.claude/skills/prep-builder/SKILL.md) | 設計案から .tfl 群を組み立て（`context: fork` で元 .tfl JSON を隔離） | ローカル（ファイル生成） |
| [prep-deployer](.claude/skills/prep-deployer/SKILL.md) | preflight（不足サブプロジェクト作成）/ publish / run / (将来) test。session intake の合意のみで一気通貫、失敗は [autonomous-recovery](.claude/skills/prep-deployer/references/autonomous-recovery.md) で自律ループ | **サーバー副作用あり（書き込み専従）** |
| [prep-output-comparator](.claude/skills/prep-output-comparator/SKILL.md) | 元フロー最終 PDS と分解後フロー最終 PDS を Metadata API + Tableau MCP で比較し、列差分と全体行数差分を Markdown で出力（原因分析・修正提案・値同値性は持たない、`context: fork`） | ローカル（ファイル生成）、Cloud は **読み取りのみ** |

役割対称性: **読み取り = prep-extractor + prep-output-comparator / 書き込み = prep-deployer**。Cloud 状態スナップショット (`deploy-context.md`) を extractor が用意して deployer が消費。Publish 完了後の DS 比較は comparator が独立に Cloud を読み、結果をメインエージェントに返す。修正判断は comparator の出力を元にメインエージェントが prep-builder / prep-deployer を再呼び出しする (comparator は修正には踏み込まない)。

## work/ ディレクトリ規約

このリポジトリ内で動くときの **セッションスコープの作業ディレクトリ**。各セッションの全成果物 (Skill 出力 / .tfl / build スクリプト) を集約する公式の置き場。「スクラッチ (使い捨ての遊び場)」ではない。

ユーザー自身の Prep プロジェクトで Skill を使う場合 (= リポ外コンテキスト) は別構造。詳細は [prep-builder SKILL.md](.claude/skills/prep-builder/SKILL.md) 参照。判定の境界: 作業場所が `<this-repo>/` の内側 → ここで規定する `work/` 配下、外側 → ユーザー Prep プロジェクト直下。

命名: `work/<yyyymmdd>_<tag>/` (`<tag>` は Session intake の [Q3](#session-intake-step-0) で決まる)

直下は **入力 + 4 サブフォルダ** で固定。Skill ごとにフォルダを切るのではなく、**ファイルの「役割」で分離** する (Skill が増えても直下が膨張しない):

```
work/<yyyymmdd>_<tag>/
├── <original>.tfl                 # 入力: 元 .tfl (DL したもの)
├── flow.json                      # 入力: 展開済 flow.json
├── reports/                       # Skill が生成する MD/JSON すべて
├── flows/                         # prep-builder の .tfl 成果物 (staging/intermediate/marts)
├── scripts/                       # 再現用の公式スクリプト (build_tfls.py 等、冪等)
└── scratch/                       # セッション中の試行錯誤・使い捨て
```

| サブフォルダ | 入れるもの | 入れないもの |
|---|---|---|
| `reports/` | prep-extractor の `flow-summary.md` / `deploy-context.md`、prep-architect の `analysis-*.md` / `decomposition-plan-*.md`、prep-output-comparator の `comparison-report.md` / `pairs.json` | スクリプト、.tfl |
| `flows/` | prep-builder の `staging/*.tfl` / `intermediate/*.tfl` / `marts/*.tfl` | レポート、試行錯誤の .tfl |
| `scripts/` | **公式の再生成スクリプト** (例: `build_tfls.py` — このセッションの .tfl 群を再ビルドできるもの)。冪等で再実行可能 | 1 回限りの修正試行・実験 |
| `scratch/` | 試行錯誤・使い捨ての py / メモ (例: `patch_target_path.py`, `fix_failures.py`, 検証用 `regression_test_*.py`) | 後段の Skill が依存するスクリプト |

迷ったら: 機械生成 MD/JSON → `reports/` / .tfl 成果物 → `flows/` / 再ビルド時に再実行する公式 → `scripts/` / その他 → `scratch/`。

git 追跡: `work/README.md` を除き **追跡外**。各セッションが個別のもので、リポ本体には属さないため。固まった知見は適切な場所に **昇格** させる: 規約 → CLAUDE.md / 判断基準 → Skill `references/` / 実装 → `scripts/` (横断) または Skill `scripts/` (専用)。

**このリポジトリの直下に `flows/` / `models/` 等のデータディレクトリを作らない**。理由: このリポは **Skill 配布専用** でデータ実体はバージョン管理対象外。リポ直下の `flows/` は配布物とデータ実体を混在させ、`.gitignore` 漏れや配布物の肥大を招く。

## Repo 構造

ディレクトリ構造と scripts / references の配置ルールは [references/repo-layout.md](references/repo-layout.md) に分離。判断基準は「2 つ以上の Skill が使うなら repo 直下、単一 Skill 内で完結するなら Skill 配下」。

## 認証情報の運用

REST 認証は PAT、`.env` 経由 ([.env.template](.env.template) 参照、実 `.env` は `.gitignore` 済)。実装は [scripts/tableau_auth.py](scripts/tableau_auth.py)、詳細運用 (PAT 発行・失効・トラブル対応) は [prep-deployer/references/authentication.md](.claude/skills/prep-deployer/references/authentication.md)。
