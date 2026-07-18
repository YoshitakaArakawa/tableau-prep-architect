# tableau-prep-architect

Tableau Prep の長大フローを、dbt 流のレイヤ規律で分解・再構築するための Claude Code エージェントです (11 の Skill が連携し、Cloud publish と元フローとの parity 比較、さらにスケジュール設計・Workbook 参照置換まで支援します)。

## これは何か

- 巨大化した Tableau Prep フロー (.tfl/.tflx) を **extract → analyze → decompose → build → publish → compare** のパイプラインで再構築するための AI エージェント補助ツールです。中核は `analyze → decompose → build`、必要に応じて Cloud publish と元フローとの parity 比較まで一気通貫で走らせられます
- dbt の **staging / intermediate / marts** というレイヤ分割と命名規約を Prep に転用します
- dbt 自体は使いません——コンセプトのみを借りています

## 設計思想 / 使いどころ

このリポジトリは dbt のレイヤ規律のエッセンスを Prep に持ち込むための道具であり、**本来 DWH 側でやるべき data modeling を Prep で代替するためのものではありません**。

理想形は、**DWH 側で staging → intermediate → marts まで構築済みで**、Tableau には mart 以降だけが Published Data Source として連携されている状態です。Prep の出力は基本的に Hyper Extract として全行マテリアライズされるため、**DWH 側で View / Materialized View / dbt model として実現できるなら、常にそちらの方が安く済みます**（ストレージ・再計算コスト・lineage 可視性、いずれの観点でも）。

このリポジトリの Skill が役立つのは、おおむね次のいずれかに該当する場合です:

- DWH 側で modeling を組む権限や組織体制が無く、Prep に押し込まざるを得ない
- すでに長大化した Prep フローが存在していて、それを段階的に正気に保ちたい
- Python / R ステップ、Prep 固有のピボット処理など、本当に Prep にしか書けない処理が含まれている

### Mart 層は fct / dim / rpt の三本立て

dbt のベストプラクティスを Prep に転用しますが、Tableau Workbook には **Published Data Source 同士を Relationship / Join できない** という制約があります（結合は Data Blending のみで、非加法集計に弱いという特性があります）。そこで mart 層は、次の三本立てで構成しています:

| 種別 | 役割 |
|---|---|
| `fct_<entity>.tfl` → Published DS | 1 ファクト 1 ファイル。再利用可能な素材 |
| `dim_<entity>.tfl` → Published DS | 1 ディメンション 1 ファイル。複数 fct で共有 |
| `rpt_<scope>.tfl` → Published DS | fct × dim を Prep 内で物理 JOIN した OBT。BI が読む完成品 |

- 軽い 1 メトリック重ね合わせ程度であれば、fct/dim 直読 + Data Blending で十分です
- 複数 dim 込みの本格分析が必要であれば、rpt_*.tfl を作って結合済み Published DS を提供する形が安全です
- 事前集計が必要な場合は `agg_<entity>_<grain>` (例: `agg_revenue_monthly`) という命名をおすすめします。粒度を明示し、**atomic な fct から再計算可能**であることを保てるようにしてください

本来は DWH 側で modeling するのがコスト面で最適です。本リポジトリの Skill は「DWH 側でやれない」前提で Prep に押し込む場合の補助、という位置づけはぜひ意識していただければと思います。

### スコープ — この Skill が "やらない" こと

本 Skill は **Prep 内で完結させる前提** で動きます。以下は意図的にスコープ外としています:

- DWH 側への push-down 提案（DB View 化・仮想接続定義・DS Calculated Field 化）の自動判定・候補出力
- 既存 Prep ロジックを「DWH 側で書き直すべきか」というレイヤ別判定

これは、DWH 側で modeling を組める組織であれば本 Skill を使う前段でそちらに寄せているはずで、逆に Skill を使う状況（上記の利用条件）では DWH 側を触れない / 触らないという前提が成立しているためです。そのため push-down の検討は **Skill 利用前にユーザー側の組織判断として済んでいる** ものとして扱っており、Skill の出力にもこの軸の提案は含めていません。

## 含まれる Skill / Workflow

このリポジトリは 11 の Skill (`prep-migrate` / `prep-extractor` / `prep-architect` / `prep-builder` / `prep-deployer` / `prep-output-comparator` / `prep-pds-augmenter` / `prep-schedule-designer` / `prep-workbook-repointer` / `prep-pds-backfiller` / `prep-migration-planner`) で構成されています。Workflow 全体図・Session intake・起動順序は entry-point skill [prep-migrate](.claude/skills/prep-migrate/SKILL.md)、各 Skill の役割と副作用区分は [CLAUDE.md](CLAUDE.md#skill-構成) にまとめてあります（CLAUDE.md は Agent 起動時に自動ロードされ、prep-migrate は移行系の依頼を受けたセッション冒頭に CLAUDE.md の起動規則で invoke されます）。

`prep-migrate` は移行セッションの **entry-point** で、ユーザーが分析・分解・移行・publish・比較・スケジュール・repoint・backfill を依頼したときにセッション冒頭で起動され、intake (Q1-Q5)・workflow・停止点 (Stop 1/2) の手順に沿って他の Skill を正しい順序で呼び出します。フロー内設計 (`prep-architect`) やセッション横断の計画台帳 (`prep-migration-planner`) には踏み込みません。

うち `prep-pds-augmenter` は他 Skill から呼ばれる横断ユーティリティで、Published Data Source の column-level 改変 (rename / cast / hide) + Calculated Field 注入を担います。stg レイヤを .tfl ではなく Live PDS で表現するときに使われます。

`prep-schedule-designer` は移行後の新フロー群の定期実行 (Linked Task) を設計・検証する読み取り専用 Skill です。Linked Task は Cloud UI 専用で REST 作成できないため、人間が UI でセットアップするための設計資料を出し、作成後にサーバー実測と突合します。

`prep-workbook-repointer` は移行後、旧 PDS を参照する Workbook を新 marts PDS へ差し替える Skill です。design (設計資料の生成) / repoint (TWB 手術による自動差し替え。リハーサル publish → 証拠レポート承認 → 本番 Overwrite の段取りゲート付き。サーバー書込) / verify (lineage 反映の検証) の 3 モードを持ち、人間が Tableau Desktop の Replace Data Source で差し替える経路も runbook で支援します。

`prep-pds-backfiller` は移行完了後の**任意の後工程**です。incremental フローを分解すると新しい accumulator PDS は最新バッチしか持たないため、旧 output PDS にしか残っていない過去の累積履歴を hyper-level surgery で一度だけ seed します。取り消しにくい本番 Overwrite を含むので、dry-run → sandbox preview → 明示承認 → 本番 → 受け入れ incremental の段取りゲートを踏みます。

`prep-migration-planner` は複数フロー or 横断工程 (スケジュール / repoint / backfill) を含む移行の scope・移行順・人間作業キュー・進捗を 1 枚に集約するオーケストレーション台帳 (migration-plan.md + .json) を生成・更新します。フロー内設計には踏み込まず、セッション横断の resume state も兼ねます。

## 既知の制限

### Cloud 上で flow のプレビュー画像が出ない ("Flow image unavailable")

本 Skill が生成して publish した flow は、Tableau Cloud の Overview / プロジェクト一覧で `Flow image unavailable` のままになります (DAG プレビュー画像が表示されません)。

- 原因: tableauserverclient の `server.flows.publish()` 経由の publish では、Cloud 側で flow image が生成されないという既知バグです ([server-client-python #1537](https://github.com/tableau/server-client-python/issues/1537), 2024-11 報告、現在 open)
- 回避策の調査結果:
  - Web Authoring (Cloud 上で "Edit Flow" → 保存) → 効果はありませんでした
  - 元 .tfl の `flowGraphImage.png` / `flowGraphThumbnail.svg` を generated .tfl に同梱して再 publish → 画像自体は表示されるものの、**元 flow の DAG = 分解前の全体絵** が表示されてしまい実態と乖離します (誤誘導のリスクがあるため採用していません)
  - Tableau REST API に flow thumbnail を GET / 再生成する endpoint は存在しません ([Flow Methods](https://help.tableau.com/current/api/rest_api/en-us/REST/rest_api_ref_flow.htm))
- 確実に正しい画像を出す唯一の手段は、Tableau Prep Builder Desktop で各 .tfl を開いて save し、そこから手動 publish する方法です (Builder 経由の publish はこのバグの対象外です)
- Skill のスコープ: 自動での画像生成・同梱は行いません。表示が必要な場合は手動での Builder publish をおすすめします

## 認証

REST API への認証は、OAuth 2.0 (Authorization Code + PKCE) によるブラウザサインインを採用しています。`.env` ファイルには `SERVER` と `SITE_NAME` のみを置き、secret は持ちません。初回および token 失効後はブラウザで Tableau Cloud のサインイン画面が自動で開きます。テンプレートは [.env.template](.env.template) を参照してください。実 `.env` は `.gitignore` で除外しています。

CI/CD などの非対話実行が必要な場合は、本リポのスコープ外として別途 PAT ベースの簡易スクリプトを切り出す前提です。

## 使い方

1. このリポジトリを Claude Code が認識できる場所に配置します（または plugin として配布します）
2. ご自身の Prep プロジェクトで Claude Code を起動します
3. 既存 .tfl を指して分解依頼を出します。Agent は以下の項目を **1 ターンでまとめて** 確認します:
   - **Q1. 元フローの所在** — ローカル `.tfl/.tflx` パス、または Cloud 上の flow 名 / URL / LUID
   - **Q2a. ゴール深度** — ① 分析だけ / ② 分解設計まで / ③ .tfl 生成まで / ④ Cloud に publish & run まで / ⑤ 元フローとの E2E parity 比較まで
   - **Q2b. 横断工程** — schedule / repoint / backfill の複数選択（省略可）
   - **Q3. 作業フォルダ名** — `work/<yyyymmdd>_<tag>/` の tag 部分
   - **Q4. publish 先 project path** — Q2a が ② 以上で必須
   - **Q5. 既存 migration-plan** — 複数セッションに跨る移行を再開するときのみ（前セッションの `migration-plan.json` パス、新規は空欄）
4. 合意後は一気通貫で進みます。`extract → analyze → decompose`（ゴールが ④ 以上なら decompose の前に Cloud 側プロジェクトを作る preflight を挟みます）を自動で走らせ、複数フロー・横断工程を含む移行では冒頭に薄い Stop 1 を 1 回だけ挟みます。その後 **decompose 完了時に 1 回だけユーザー確認 (Stop 2)** を取り、`OK` で `build → publish → run → compare` まで自律実行します
5. 失敗時は AI が原因を機械判定し、回復可能な種別 (publish errorCode 280003、name conflict 409、上流 PDS 不在等) は自律ループでリトライします。認証 / 権限 / 容量など回復不能な種別はユーザーに escalation されます

ローカル成果物のみで止めたい場合 (Q2a が ②/③) は `/prep-architect` や `/prep-builder` を個別に呼ぶこともできます。出力された新 .tfl は Tableau Prep Builder で開いて検証可能です。

詳細な前提のうち Session intake の各質問項目・Workflow・停止点は [prep-migrate](.claude/skills/prep-migrate/SKILL.md)、命名規約・推奨フォルダ構造・配置規約は [CLAUDE.md](CLAUDE.md) を参照してください。

## Codex で使う

この Skill 集は OpenAI Codex からも利用できます。Skill 本体は `.claude/skills/` が **正典** で、Codex 向けには薄い入口だけを追加しています。Codex は Anthropic 型 Agent Skills (SKILL.md + YAML frontmatter) をサポートし、リポジトリルートの `AGENTS.md` と `.agents/skills/` を入口として読みます。

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
2. **Tableau MCP を設定する** — parity 比較 (`prep-output-comparator`) などが Tableau MCP のツールを使います。`.codex/config.toml.template` を `.codex/config.toml` にコピーし (`.gitignore` 済、`.env` / `.env.template` と同じ運用)、`[mcp_servers.tableau]` テンプレートを自分の環境の Tableau MCP 実装に合わせて有効化するか、ユーザーの Codex config 側で設定します。本リポジトリは特定の MCP 実装・起動コマンドを規定しません
3. **認証は Claude Code と共通** — `.env` に `SERVER` / `SITE_NAME` のみ置きます ([.env.template](.env.template) 参照)。OAuth のブラウザサインインは Claude Code と同じ仕組みです

詳細は [.codex/README.md](.codex/README.md) を参照してください。

### 使い方

`$prep-migrate` のように `$skill-name` で明示起動するか、移行系の自然言語依頼で暗黙起動します。移行系の依頼を受けたら、まず `.claude/skills/prep-migrate/SKILL.md` を読み、その intake・workflow・停止点の手順に従います。

`.agents/skills/<name>/SKILL.md` は Codex 向けの入口 (thin wrapper) で、正典 `.claude/skills/<name>/SKILL.md` を読み、`AGENTS.md` の読み替え表に従って実行するよう促すだけです。fork 系 Skill は `.codex/agents/` の `flow-worker` / `flow-worker-lite` サブエージェントに委譲して隔離実行します。

### 制約

- **fork 相当はサブエージェント委譲で近似** します。サブエージェントが使えない環境ではインライン実行してよいですが、その場合も出力契約 (成果物はファイルへ書く・主会話へ中間 JSON を流さない・返答は要約 + `## Timing` ブロックのみ) は維持します。完全な文脈隔離は保証されません
- **`allowed-tools` 相当の制御はありません**。ツール実行は Codex の approval / sandbox 設定に従います
- サブエージェント機能はバージョン差が大きいため、利用できない場合は上記のインライン実行にフォールバックしてください

## ライセンス

[MIT License](LICENSE)
