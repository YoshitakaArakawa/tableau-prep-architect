# tableau-prep-architect

Tableau Prep の長大フロー (.tfl/.tflx) を、dbt 流のレイヤ規律 (staging / intermediate / marts) で分析・分解・再構築するための Claude Code エージェント環境です。

- [これは何か](#これは何か)
- [設計思想 / 使いどころ](#設計思想--使いどころ)
- [Skill 構成と移行ワークフロー](#skill-構成と移行ワークフロー)
- [認証](#認証)
- [使い方](#使い方)
- [Codex で使う](#codex-で使う)
- [既知の制限](#既知の制限)
- [ライセンス](#ライセンス)

## これは何か

- 巨大化した Prep フローを **extract → analyze → decompose → build → publish → compare** のパイプラインで再構築します。中核は analyze → decompose → build です。必要なら Cloud publish と元フローとの parity 比較まで一気通貫で走らせられます
- 移行後の後工程 — スケジュール設計、Workbook / Tableau Pulse の参照置換、履歴 backfill — も Skill として備えます
- dbt の **staging / intermediate / marts** というレイヤ分割と命名規約を Prep に転用します。dbt 自体は使いません——コンセプトのみ借りています

## 設計思想 / 使いどころ

本リポジトリは dbt のレイヤ規律のエッセンスを Prep に持ち込むための道具であり、**本来 DWH 側でやるべき data modeling を Prep で代替するためのものではありません**。

理想形は、**DWH 側で staging → intermediate → marts まで構築済み** の状態です。そこでは Tableau に mart 以降だけが Published Data Source として連携されます。一方 Prep の出力は基本的に Hyper Extract として全行マテリアライズされます。DWH 側で View / Materialized View / dbt model として実現できるなら、常にそちらの方が安く済みます — ストレージでも、再計算コストでも、lineage 可視性でも。

要するに: DWH でやれるなら DWH でやる。やれないときの道具が本リポジトリです。

役立つのは、おおむね次のいずれかに該当する場合です:

- DWH 側で modeling を組む権限や組織体制が無く、Prep に押し込まざるを得ない
- すでに長大化した Prep フローが存在していて、それを段階的に正気に保ちたい
- Python / R ステップ、Prep 固有のピボット処理など、本当に Prep にしか書けない処理が含まれている

### Mart 層は fct / dim / rpt の三本立て

Tableau Workbook には **Published Data Source 同士を Relationship / Join できない** という制約があります。Workbook 側でできる結合は Data Blending のみで、非加法集計に弱い特性があります。そこで mart 層を次の三本立てで構成します:

| 種別 | 役割 |
|---|---|
| `fct_<entity>.tfl` → Published DS | 1 ファクト 1 ファイル。再利用可能な素材 |
| `dim_<entity>.tfl` → Published DS | 1 ディメンション 1 ファイル。複数 fct で共有 |
| `rpt_<scope>.tfl` → Published DS | fct × dim を Prep 内で物理 JOIN した OBT。BI が読む完成品 |

使い分けの目安:

- 軽い 1 メトリックの重ね合わせ程度なら、fct/dim 直読 + Data Blending で足ります
- 複数 dim 込みの本格分析には、`rpt_*.tfl` で結合済み Published DS を提供する形が安全です
- 事前集計が必要な場合は `agg_<entity>_<grain>` (例: `agg_revenue_monthly`) と命名します。粒度を明示し、**atomic な fct から再計算可能** に保ちます

### スコープ — 本リポジトリが "やらない" こと

本リポジトリは **Prep 内で完結させる前提** で動きます。以下は意図的にスコープ外です:

- DWH 側への push-down 提案 (DB View 化・仮想接続定義・DS Calculated Field 化) の自動判定・候補出力
- 既存 Prep ロジックを「DWH 側で書き直すべきか」というレイヤ別判定

理由は利用条件の裏返しです。DWH 側で modeling を組める組織なら、本リポジトリを使う前段でそちらに寄せているはずです。逆に本リポジトリを使う状況では、DWH 側を触れない / 触らない前提が成立しています。そのため push-down の検討は **利用前にユーザー側の組織判断として済んでいる** ものとして扱い、出力にもこの軸の提案は含めません。

## Skill 構成と移行ワークフロー

11 の Skill は 2 群に分かれます。

**中核パイプライン (5)** — この順に実行され、extract から parity 比較までを分担します:

1. `tableau-prep-extractor` — フローと Cloud 情報の抽出
2. `tableau-prep-architect` — 分析・分解設計
3. `tableau-prep-builder` — 新 .tfl 群の組み立て
4. `tableau-prep-deployer` — publish / run
5. `tableau-pds-comparator` — 元フローとの parity 比較

**移行後の横断工程 (6)** — 必要なものだけ使います:

- `tableau-prep-schedule-designer` — スケジュール設計
- `tableau-workbook-repointer` — Workbook 参照置換
- `tableau-pulse-repointer` — Pulse 参照置換
- `tableau-pds-backfiller` — 履歴 seed
- `tableau-pds-augmenter` — PDS 改変ユーティリティ
- `tableau-prep-migration-planner` — 複数フロー移行の台帳

各 Skill の役割と副作用区分の一覧は [CLAUDE.md](CLAUDE.md#skill-構成) が正典です (ここには再掲しません)。

移行セッションの **entry-point** は [references/migration-workflow.md](references/migration-workflow.md) です。分析・分解・移行系の依頼を受けると、Agent はセッション冒頭でこの手順書を読みます。以後は手順書の intake と停止点に沿って、各 Skill を正しい順序で呼び出します。

## 認証

REST API への認証は、OAuth 2.0 (Authorization Code + PKCE) によるブラウザサインインです。初回および token 失効後は、ブラウザで Tableau Cloud のサインイン画面が自動で開きます。

- `.env` には `SERVER` と `SITE_NAME` のみを置きます。secret は持ちません
- テンプレートは [.env.template](.env.template)。実 `.env` は `.gitignore` で除外済みです
- CI/CD などの非対話実行は本リポジトリのスコープ外です。必要なら別途 PAT ベースの簡易スクリプトを切り出す前提です

## 使い方

1. このリポジトリを clone し、Claude Code をリポジトリのルートで起動します。Skill 群 + 起動規則 + work/ 規律 + 共通 scripts + Codex 入口を一体で使います (中身を参考にするだけでも構いません)
2. 認証用の `.env` をリポジトリ直下に置きます。移行の成果物は `work/<yyyymmdd>_<tag>/` 配下に生成されます
3. 既存 .tfl を指して分解依頼を出します。Agent が **元フローの所在 / ゴール深度 / 横断工程の有無 / 作業フォルダ名 / publish 先** などを 1 ターンでまとめて確認します (項目の正典は [migration-workflow.md の Session intake](references/migration-workflow.md#step-0-session-intake))
4. 停止点は最大 2 回です:
   - **Stop 1 (計画承認)** — 複数フロー・横断工程を含む移行のみ。冒頭に 1 回
   - **Stop 2 (設計承認)** — すべての移行で decompose 完了時に 1 回
5. Stop 2 の `OK` 後は `build → publish → run → compare` まで自律実行します。ゴールが publish 以上なら、decompose の前に Cloud 側プロジェクトを作る preflight が自動で入ります
6. 失敗時は原因を機械判定します。回復可能な種別 (例: publish errorCode 280003、name conflict 409、上流 PDS 不在) は自律リトライします。認証 / 権限 / 容量など回復不能な種別はユーザーに escalation します

ローカル成果物のみで止めたい場合は、`/tableau-prep-architect` や `/tableau-prep-builder` を個別に呼ぶこともできます。出力された新 .tfl は Tableau Prep Builder で開いて検証可能です。

命名規約・推奨フォルダ構造・配置規約は [CLAUDE.md](CLAUDE.md) を参照してください。

## Codex で使う

本リポジトリは OpenAI Codex からも利用できます。Skill 本体は `.claude/skills/` が **正典** で、Codex 向けには薄い入口だけを追加しています。Codex は Anthropic 型 Agent Skills (SKILL.md + YAML frontmatter) をサポートします。入口として、リポジトリルートの `AGENTS.md` と `.agents/skills/` を読みます。

### 前提

- Skills に対応したバージョンの Codex CLI / IDE
- `context: fork` や `allowed-tools` など Claude Code 固有の frontmatter は Codex には存在しません。これらは `AGENTS.md` の「Claude Code 記法の読み替え表」で吸収します (Skill ファイル自体は書き換えません)

### セットアップ

1. **リポジトリを trusted にする** — `.codex/` 配下 (config.toml / サブエージェント定義) は trusted プロジェクトでのみ有効です。ユーザーの Codex 設定 (`~/.codex/config.toml`) でこのリポジトリのクローン先を trusted に登録します:

   ```toml
   [projects."/path/to/tableau-prep-architect"]
   trust_level = "trusted"
   ```

   (パスはプレースホルダ。各自の絶対パスに置き換えてください。)
2. **Tableau MCP を設定する** — parity 比較 (`tableau-pds-comparator`) などが Tableau MCP のツールを使います。`.codex/config.toml.template` を `.codex/config.toml` にコピーします (`.gitignore` 済、`.env` / `.env.template` と同じ運用)。その中の `[mcp_servers.tableau]` テンプレートを、自分の環境の Tableau MCP 実装に合わせて有効化します。ユーザーの Codex config 側で設定しても構いません。本リポジトリは特定の MCP 実装・起動コマンドを規定しません
3. **認証は Claude Code と共通** — `.env` に `SERVER` / `SITE_NAME` のみ置きます ([.env.template](.env.template) 参照)。OAuth のブラウザサインインは Claude Code と同じ仕組みです

詳細は [.codex/README.md](.codex/README.md) を参照してください。

### 使い方

移行系の依頼を受けたら、まず [references/migration-workflow.md](references/migration-workflow.md) を読みます。以後はその intake・workflow・停止点の手順に従います。個別 Skill は `$skill-name` で明示起動するか、自然言語依頼で暗黙起動します。

`.agents/skills/<name>/SKILL.md` は Codex 向けの薄い入口 (thin wrapper) です。内容は、正典 `.claude/skills/<name>/SKILL.md` を読んで `AGENTS.md` の読み替え表に従うよう促すだけです。fork 系 Skill は `.codex/agents/` の `flow-worker` / `flow-worker-lite` サブエージェントに委譲して隔離実行します。

### 制約

- **fork 相当はサブエージェント委譲で近似** します。完全な文脈隔離は保証されません
- サブエージェントが使えない環境 (バージョン差が大きい) では、インライン実行にフォールバックしてよいです。その場合も出力契約は維持します: 成果物はファイルへ書く / 主会話へ中間 JSON を流さない / 返答は要約 + `## Timing` ブロックのみ
- **`allowed-tools` 相当の制御はありません**。ツール実行は Codex の approval / sandbox 設定に従います

## 既知の制限

### Cloud 上で flow のプレビュー画像が出ない ("Flow image unavailable")

本リポジトリが publish した flow は、Tableau Cloud の Overview / プロジェクト一覧で `Flow image unavailable` と表示され、DAG プレビュー画像が出ません。原因は、tableauserverclient の `server.flows.publish()` 経由では Cloud 側で flow image が生成されないという既知バグです ([server-client-python #1537](https://github.com/tableau/server-client-python/issues/1537), 2024-11 報告、現在 open)。

正しい画像を確実に出す唯一の手段は、Tableau Prep Builder Desktop で各 .tfl を開いて save し、そこから手動 publish することです (Builder 経由の publish はこのバグの対象外)。本リポジトリは自動での画像生成・同梱を行いません。

<details>
<summary>調査済みの回避策 (いずれも不採用)</summary>

- Web Authoring (Cloud 上で "Edit Flow" → 保存) — 画像は生成されません
- 元 .tfl の `flowGraphImage.png` / `flowGraphThumbnail.svg` を generated .tfl に同梱して再 publish — 画像は表示されますが、**分解前の全体 DAG** が表示されて実態と乖離するため誤誘導のリスクがあります
- Tableau REST API — flow thumbnail を GET / 再生成する endpoint は存在しません ([Flow Methods](https://help.tableau.com/current/api/rest_api/en-us/REST/rest_api_ref_flow.htm))

</details>

## ライセンス

[MIT License](LICENSE)
