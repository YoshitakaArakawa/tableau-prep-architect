# tableau-prep-architect

## Overview

このリポジトリは Tableau Prep の長大化したフロー (.tfl/.tflx) を、dbt 流のレイヤ規律（staging / intermediate / marts）で **分析・分解設計・再構築** するための Claude Code Skill 集。**dbt 自体は使わない**——コンセプトのみ転用。

詳細な思想・利用条件・スコープ外（push-down 提案など）は [README.md](README.md#設計思想--使いどころ) 参照。

## 成果物の置き場

extract / analyze / decompose / build / preflight の **全 Skill 出力 (markdown レポート、`.tfl`、`deploy-context.md`、build スクリプト等)** は、作業コンテキストによって 2 つの置き場を使い分ける:

| コンテキスト | 置き場 | 用途 |
|---|---|---|
| **(A) このリポジトリ内で作業** (architect 開発、Skill iterate、検証ループ) | `work/<yyyymmdd>_<tag>/` 配下にすべて (Skill 出力も `.tfl` も同じセッションフォルダ内) | 今セッションのような「flow を持ち込んで分解する」作業もこちら。詳細は [work/ ディレクトリ規約](#work-ディレクトリ規約) |
| **(B) ユーザー自身の Prep プロジェクトで Skill を使う** (downstream 利用) | プロジェクト直下の `flows/{staging,intermediate,marts}/` | Skill が production 用に組み込まれた使い方。詳細は [ユーザー作業フォルダ規約](#ユーザー作業フォルダ規約) |

**このリポジトリの直下に `flows/` / `models/` 等のデータディレクトリを作らない**。理由: このリポは **Skill 配布専用** で、データ実体はバージョン管理対象外という思想。リポ直下の `flows/` は「Skill 配布物」と「データ実体」を混在させ、`.gitignore` 漏れや配布物の肥大を招く。

判定が曖昧なときは「作業場所が `<this-repo>/` の内側なら (A)、外側なら (B)」で OK。

## Claude がこのリポジトリで助けること

ユーザーが既存 Prep フローを指して「分析して」「分解設計して」「dbt 風に整理して」「Tableau Cloud に publish して」「実行して」と指示したとき、各 Skill を **順次または個別に** 実行する。**Session intake (step 0) で goal / target path を確定したら、その先は extract → analyze → decompose → build → preflight → publish → run まで段階間の承認を取らず一気通貫で進める**。失敗時は AI が原因を機械判定し、回復可能な種別 (例: 280003 → re-build / 409 → Overwrite / 上流 PDS 不在 → 上流 republish) は自律ループでリトライ、回復不能な種別 (認証 / 権限 / 容量 / Cloud 障害 / loop 検知発火) は escalation。詳細は [autonomous-execution-policy](.claude/skills/prep-deployer/references/autonomous-execution-policy.md) と [autonomous-recovery](.claude/skills/prep-deployer/references/autonomous-recovery.md)。

## Workflow

```
[step 0]  Session intake (会話)                    各 Skill を呼ぶ前に、メインエージェントが
                                                   ユーザーから以下 4 点をまとめて聞く（詳細は後述）。
                                                   ・元フロー所在  ・ゴール段階  ・作業フォルダ名
                                                   ・target path (ゴール② 以上で必要)
                ↓

[step 0a] prep-extractor ─ get-project-structure   ユーザーが提示する target パス（任意深さ）を REST API で
                                                   walk し、existing prefix と pending segments に分割
                                                   → deploy-context.md（読み取りのみ）
                ↓ (deploy-context.md は decompose / preflight / publish で消費)

[step 0b] prep-deployer ─ preflight    pending segments を 1 個ずつ create_project.py で作成、
                                       最後に target 配下に stg/intermediate/marts を create_projects.py で作成

prep-extractor ─ flow-extract   .tfl/.tflx → flow-summary.md（構造抽出）
        ↓
prep-architect ─ analyze        現状把握 → analysis-<flow>.md
                                          （deploy-context.md があれば既存flow名衝突も加味）
prep-architect ─ decompose      分解設計 → decomposition-plan-<flow>.md
        ↓
prep-builder ─ build            .tfl 群を生成
                                必ず元 .tfl の maestroMetadata / displaySettings を新 .tfl に同梱
        ↓
prep-deployer ─ publish + run   レイヤ単位で publish → run → finishCode=0 確認
                                stg レイヤ完走 → int レイヤ → marts レイヤの順 (上流 PDS が下流 Input)
                                同一レイヤ内は並列可、レイヤ間は必ず順次
                                失敗時は autonomous-recovery のマッピングで分類 → 自律リトライ or escalation
        ↓
prep-deployer ─ test (将来)      VDS でデータ品質テスト
```

**publish 先構造のモデル**:
- 最下層は規約固定 (`stg / intermediate / marts`)
- target = それら 3 つの直上のプロジェクト
- target の上は任意の深さ・任意の命名（`99_Sandbox/Q4-2026/...` 等、ユーザー文脈次第）
- 不足分は preflight が **idempotent に一括作成** する（session intake の target path 指定が合意済みなので追加承認なし）
- 各レイヤの Output Published DS は、その flow が置かれる layer project と同じ場所に書く (stg flow → `<target>/stg` に PDS / int → `<target>/intermediate` / marts → `<target>/marts`)

ユーザーが path ではなく自然言語で構造を指示する場合は AI/Skill 層で path に変換してから 0a を呼ぶ。

step 0a / 0b は最初に一度走らせれば良く、その後の analyze / decompose / build を反復するときは `deploy-context.md` を再利用する。Cloud 側の構造が変わったら 0a だけ再実行。

## Session intake (step 0)

各 Skill は「必要な入力が会話に既に出ている」前提で動く。逆に言うと、メインエージェントが Skill を呼び始める前に、必要な入力を **1 ターンでまとめてユーザーに聞いておく** べき。遅延収集（必要になった時点で個別に聞く）は確認往復が増えるので避ける。

セッション冒頭で聞く 4 項目:

| # | 質問 | 必須条件 | 受け取り後の使い道 |
|---|---|---|---|
| **Q1. 元フローの所在** | ローカル `.tfl/.tflx` パス、または Tableau Cloud 上の flow 名 / URL / LUID | 常に必須 | Phase A 入力。サーバー DL するなら `list_flows.py` → `download_flow.py` |
| **Q2. ゴール段階** | ① 分析だけ / ② 分解設計まで / ③ .tfl 生成まで / ④ Cloud に publish & run まで | 常に必須 | ここで ④ を選んだことが publish / run の合意になる (以後は自律ループで進む)。必要インプット・`.env` 要否も決める |
| **Q3. 作業フォルダ名** | `work/<yyyymmdd>_<タグ>/` の `<タグ>` 部分（空欄なら AI が Q1 フロー名から自動生成 → 復唱確認） | 常に必須 | **そのセッションの全成果物の置き場** ([成果物の置き場](#成果物の置き場) の context A)。Skill markdown 出力 (flow-summary / analysis / decomposition-plan / deploy-context) も **build した `.tfl` 群 (`work/.../flows/{staging,intermediate,marts}/`)** も build スクリプトも、すべてここに入れる |
| **Q4. target path** | publish 先プロジェクトの path（例: `99_Sandbox/flow241407_decompose`、任意深さ可）または target LUID | Q2 が ② 以上で必須（② でも既存 flow 名衝突回避に有用） | step 0a (`get_project_structure.py --project-path`) の入力 |

補足ルール:

- **Q4 を自然言語で答えられたとき** (例: 「99_Sandbox の下に decompose 用のフォルダを作って」): メインエージェントが path に変換する責務を持つ。手順は (1) 必要なら `get_project_structure.py --project-path "99_Sandbox"` で既存階層を確認、(2) ユーザー意図を path に復元、(3) 「`99_Sandbox/flow241407_decompose` に作りますか?」と復唱して合意、(4) 確定 path を引数に step 0a を呼ぶ。`prep-extractor` は確定済み path しか受けない (NL 解釈は会話で完結)
- **Q1 で URL の数値 ID を渡されたら**: それは vizportalUrlId で REST から直接引けないので、`list_flows.py --url-contains <数値>` で LUID を逆引きする
- **Q4 で project URL (`projects/<数値>`) を渡されたら**: project の vizportalUrlId は REST / Metadata API のいずれからも逆引き不可（[prep-extractor SKILL.md §URL ID 解決について](.claude/skills/prep-extractor/SKILL.md#url-id-解決について)）。flow URL のように逆引きを試みず、ユーザーに project name または `Parent/Child` path を聞き直す
- **`.env` の確認は遅延でよい**: Q2 が ③/④ または Q1 がサーバー DL のときに初めて必要になる。step 0a 実行直前に未整備なら聞く
- **聞いた内容は auto memory に保存しない**: ephemeral task details に該当する (ユーザーグローバルの auto memory 除外規約)。控えるなら `work/<date>/` 配下のメモに
- **復唱 (echo-back) は質問とは別**: Q3 タグの自動生成のように「AI が一度値を決めたあとユーザーに見せて redirect の機会を与える」動作は質問 (= ユーザーの判断を待つ) ではない。**no-clarifying-questions モード下でも省略しない**。「タグは `stock-market-prep` で進めます」と 1 行宣言するだけで、ユーザーは違えば訂正でき、合っていればそのまま進む

## Skill 構成

| Skill | 役割 | 副作用 |
|---|---|---|
| [prep-extractor](.claude/skills/prep-extractor/SKILL.md) | Phase A: flow.json → flow-summary.md / Phase B: Cloud project hierarchy → deploy-context.md（`context: fork` で大きな JSON を隔離） | ローカル（ファイル生成）、Cloud は **読み取りのみ** |
| [prep-architect](.claude/skills/prep-architect/SKILL.md) | analyze（業務解釈・レイヤ推定）+ decompose（分解設計、deploy-context があれば名前衝突も加味） | ローカル（ファイル生成） |
| [prep-builder](.claude/skills/prep-builder/SKILL.md) | 設計案から .tfl 群を組み立て（`context: fork` で元 .tfl JSON を隔離） | ローカル（ファイル生成） |
| [prep-deployer](.claude/skills/prep-deployer/SKILL.md) | preflight（不足サブプロジェクト作成）/ publish / run / (将来) test。session intake の合意のみで一気通貫、失敗は [autonomous-recovery](.claude/skills/prep-deployer/references/autonomous-recovery.md) で自律ループ | **サーバー副作用あり（書き込み専従）** |

役割対称性: **読み取り = prep-extractor / 書き込み = prep-deployer**。Cloud 状態スナップショット (`deploy-context.md`) を extractor が用意し、deployer はそれを消費して書き込み判断のみ行う。

## Repo 構造

ディレクトリ構造と scripts / references の配置ルールは [references/repo-layout.md](references/repo-layout.md) に分離。判断基準は「2 つ以上の Skill が使うなら repo 直下、単一 Skill 内で完結するなら Skill 配下」。

## ユーザー作業フォルダ規約

[成果物の置き場](#成果物の置き場) context (B) の詳細。ユーザー自身の Prep プロジェクトで Skill を使う場合のディレクトリ構造仕様は [prep-builder SKILL.md §context (B) ユーザー Prep プロジェクトの想定構造](.claude/skills/prep-builder/SKILL.md) に集約。

## 認証情報の運用

REST 認証は PAT、`.env` 経由 ([.env.template](.env.template) 参照、実 `.env` は `.gitignore` 済)。実装は [scripts/tableau_auth.py](scripts/tableau_auth.py)、詳細運用 (PAT 発行・失効・トラブル対応) は [prep-deployer/references/authentication.md](.claude/skills/prep-deployer/references/authentication.md)。

## work/ ディレクトリ規約

このリポジトリ内で動くときの **セッションスコープの作業ディレクトリ**。[成果物の置き場](#成果物の置き場) の context (A) の実体。「スクラッチ (使い捨ての遊び場)」ではなく、**そのセッションの全成果物を集約する公式の置き場**。

命名: `work/<yyyymmdd>_<tag>/` (`<tag>` は Session intake の [Q3](#session-intake-step-0) で決まる)

ここに置く (= 全部入れる):

- 元 `.tfl/.tflx` (DL したもの) と展開 `flow.json`
- prep-extractor 出力: `flow-summary.md`, `deploy-context.md`
- prep-architect 出力: `analysis-<flow>.md`, `decomposition-plan-<flow>.md`
- prep-builder 出力: `flows/{staging,intermediate,marts}/*.tfl` および build スクリプト (`build_tfls.py` 等の再ビルド用)
- セッション固有のメモ・試行錯誤

git 追跡: `work/README.md` を除き **追跡外**。これは「捨ててよい」の意味ではなく、**各セッションが個別のもので、リポ本体には属さないため**。固まった知見 (規約 / 判断基準 / 共通ロジック) は適切な場所に **昇格** させる:

- 規約 → CLAUDE.md
- 判断基準 → Skill の `references/`
- 実装 → `scripts/` (横断) または Skill の `scripts/` (専用)

例: `work/20260515_legacy-flow-analysis/`, `work/20260520_int-step-split-experiment/`
