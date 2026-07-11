# tableau-prep-architect

## Overview

このリポジトリは Tableau Prep の長大化したフロー (.tfl/.tflx) を、dbt 流のレイヤ規律（staging / intermediate / marts）で **分析・分解設計・再構築** するための Claude Code Skill 集。**dbt 自体は使わない**——コンセプトのみ転用。

詳細な思想・利用条件・スコープ外（push-down 提案など）は [README.md](README.md#設計思想--使いどころ) 参照。

## Workflow

ユーザーが既存 Prep フローを指して「分析して」「分解設計して」「dbt 風に整理して」「Tableau Cloud に publish して」「実行して」「E2E 比較して」と指示したら、各 Skill を **順次または個別に** 実行する。**Session intake (step 0) で goal / target path を確定したら、extract → analyze → decompose まで段階間の承認を取らず一気通貫。decompose 完了後に Stop 2 ユーザー確認 (1 ターン) を 1 回だけ取り、`OK` で build → preflight → publish → run → compare まで再び一気通貫**。失敗時は AI が原因を機械判定し、回復可能な種別は自律ループでリトライ、回復不能な種別 (認証 / 権限 / 容量 / Cloud 障害 / loop 検知発火) は escalation ([autonomous-recovery](.claude/skills/prep-deployer/references/autonomous-recovery.md))。compare で gap が出たらメインエージェントが報告を読み、prep-builder / prep-deployer の再呼び出しを判断する。

```
[step 0]   Session intake (会話)       Q1-Q4 を 1 ターンで聞く (§Session intake)
[Phase A]  prep-extractor              .tfl/.tflx → flow-summary.md + flow.json (構造抽出)
[step 0a]  prep-extractor Phase B      target_path walk + Input kind 分類 + PDS LUID 解決
                                       → deploy-context.md + input-dispatch-mech.json (Cloud 読み取りのみ)
[step 0b]  prep-deployer preflight     pending segments + flows/・datasources/ × dbt 3 レイヤを idempotent 作成
           prep-architect analyze      現状把握 → analysis-<flow>.md
           prep-architect decompose    分解設計 → decomposition-plan-<flow>.md (Input policy / rename 翻訳を embed)
★ Stop 2 ユーザー確認 (1 ターン) ★     plan の Tier 1 を明示確認、OK で build へ
                                       ([review-checkpoints](.claude/skills/prep-architect/references/review-checkpoints.md))
           prep-builder build          plan → 新 .tfl 群 + augmenter spec、publish-manifest.json を init
           prep-deployer publish+run   レイヤ単位 (stg → int → marts) に publish → run → finishCode=0 確認。
                                       同レイヤ内は並列可、レイヤ間は順次。manifest update、完走後 resolve-luids
           prep-output-comparator      元 PDS vs 分解後 PDS の列差分 + 全体行数差分 → Markdown
```

kind dispatch (kind=tfl は publish+run / kind=pds_augment は publish のみ)・needs_provisioning の build skip・incremental run 規律などの実装詳細は各 SKILL.md と recipe が持つ (この図には書かない)。

session manifest (`publish-manifest.json`) は 1 セッションの **元フロー LUID / 元 output PDS LUID / 分解後フローの publish & run 状態 / 分解後 output PDS LUID** をまとめた単一 JSON。形式は [references/publish-manifest-format.md](references/publish-manifest-format.md)、書き込みは prep-builder (init) + prep-deployer (update / resolve-luids)、読み取りは prep-output-comparator。

publish 先構造のモデル (target = stg/int/marts の直上、上位は任意の深さ・命名) は [references/project-hierarchy.md](references/project-hierarchy.md)。step 0a / 0b は最初に一度走らせれば良く、その後の analyze / decompose / build を反復するときは `deploy-context.md` を再利用する。

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
- URL ID 解決の詳細 (vizportalUrlId からの逆引き等) は [prep-extractor の deploy-context-procedure.md](.claude/skills/prep-extractor/references/deploy-context-procedure.md)

## Skill 構成

| Skill | 役割 | 副作用 |
|---|---|---|
| [prep-extractor](.claude/skills/prep-extractor/SKILL.md) | Phase A: flow.json → flow-summary.md / Phase B: Cloud project hierarchy + Input kind 分類 + PDS LUID 解決 → deploy-context.md + input-dispatch-mech.json（`context: fork`、mechanical only でユーザー確認なし） | ローカル（ファイル生成）、Cloud は **読み取りのみ** |
| [prep-architect](.claude/skills/prep-architect/SKILL.md) | analyze（業務解釈・レイヤ推定）+ decompose（分解設計、Input policy / rename 翻訳 / provisioning 案を embed、Stop 2 でユーザー確認）（`context: fork`） | ローカル（ファイル生成） |
| [prep-builder](.claude/skills/prep-builder/SKILL.md) | 設計案から .tfl 群を組み立て（`context: fork` で元 .tfl JSON を隔離） | ローカル（ファイル生成） |
| [prep-deployer](.claude/skills/prep-deployer/SKILL.md) | preflight（不足サブプロジェクト作成）/ publish / run。session intake の合意のみで一気通貫、失敗は [autonomous-recovery](.claude/skills/prep-deployer/references/autonomous-recovery.md) で自律ループ（fork なし — 失敗を主会話で観測するため） | **サーバー副作用あり（書き込み専従）** |
| [prep-output-comparator](.claude/skills/prep-output-comparator/SKILL.md) | 元フロー最終 PDS と分解後フロー最終 PDS を比較し、列差分と全体行数差分を Markdown で出力（原因分析・修正提案・値同値性は持たない、`context: fork`） | ローカル（ファイル生成）、Cloud は **読み取りのみ** |
| [prep-pds-augmenter](.claude/skills/prep-pds-augmenter/SKILL.md) | Published DS への calc 注入 + column transforms (rename / cast / hide)。stg を Live PDS で表現する経路で builder が spec を emit し deployer が実行 | **サーバー副作用あり（PDS publish）** |

役割対称性: **読み取り = prep-extractor + prep-output-comparator / 書き込み = prep-deployer (+ augmenter)**。extractor が用意した Cloud スナップショット (`deploy-context.md`) を deployer が消費する。comparator の比較結果を元に、メインエージェントが prep-builder / prep-deployer の再呼び出しを判断する (comparator は修正に踏み込まない)。

## work/ ディレクトリ規約

このリポジトリ内で動くセッションの全成果物 (Skill 出力 / .tfl / build スクリプト) は `work/<yyyymmdd>_<tag>/` に集約する (`<tag>` は Session intake の [Q3](#session-intake-step-0))。「スクラッチ (使い捨ての遊び場)」ではなく公式の置き場。直下は **入力 (.tfl / flow.json) + 4 サブフォルダ (`reports/` `flows/` `scripts/` `scratch/`) で固定** — ファイルの「役割」で分離し、Skill が増えても直下を膨張させない。各サブフォルダの責務 (入れるもの / 入れないもの)・昇格ルール・実行時間の事後計測 tip は [work/README.md](work/README.md) を参照。git 追跡は `work/README.md` のみ。

ユーザー自身の Prep プロジェクトで Skill を使う場合 (= リポ外コンテキスト) は別構造 ([prep-builder SKILL.md](.claude/skills/prep-builder/SKILL.md) 参照)。判定境界: 作業場所が `<this-repo>/` の内側 → `work/` 配下、外側 → ユーザー Prep プロジェクト直下。

**このリポジトリの直下に `flows/` / `models/` 等のデータディレクトリを作らない**。このリポは Skill 配布専用で、リポ直下のデータ実体は配布物との混在・`.gitignore` 漏れ・肥大を招く。

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

REST 認証は OAuth 2.0 (Authorization Code + PKCE) でブラウザサインイン。`.env` には `SERVER` / `SITE_NAME` のみを置き、secret は持たない ([.env.template](.env.template) 参照、実 `.env` は `.gitignore` 済)。実装は [scripts/tableau_auth.py](scripts/tableau_auth.py) (`signed_in_server()` context manager)。`access_token` は `<repo>/.auth-cache/session.json` (gitignore 済) にキャッシュされプロセス間で再利用される。明示破棄は `python scripts/tableau_auth.py logout`、状態確認は `python scripts/tableau_auth.py status`。詳細運用は [prep-deployer/references/authentication.md](.claude/skills/prep-deployer/references/authentication.md)。CI/CD 等の非対話用途には別途 PAT ベースの簡易スクリプトを切り出す前提。
