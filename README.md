# tableau-prep-architect

Tableau Prep の長大フローを、dbt 流のレイヤ規律で分解・再構築するための Claude Code エージェントです (4 つの Skill が連携し、自律回復にも対応しています)。

## これは何か

- 巨大化した Tableau Prep フロー (.tfl/.tflx) を **analyze → decompose → build** の 3 段階で再構築するための AI エージェント補助ツールです
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

このリポジトリは 5 つの Skill (`prep-extractor` / `prep-architect` / `prep-builder` / `prep-deployer` / `prep-output-comparator`) で構成されています。Workflow 全体図、各 Skill の役割と副作用区分、起動順序は [CLAUDE.md](CLAUDE.md#workflow) にまとめてあります（Agent 起動時に自動ロードされる真の source です）。

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
3. 既存 .tfl を指して `/prep-architect` を呼び出してください（フェーズの一部だけを実行することもできます）
4. 出力された新 .tfl を Prep Builder で開いて検証してください

詳細な前提（命名規約・推奨フォルダ構造）は [CLAUDE.md](CLAUDE.md) を参照してください。

## ライセンス

[MIT License](LICENSE)
