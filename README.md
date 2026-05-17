# tableau-prep-architect

Tableau Prep の長大フローを dbt 流のレイヤ規律で分解・再構築する Claude Code エージェント (4 Skill 連携 + 自律回復)。

## これは何か

- 巨大化した Tableau Prep フロー (.tfl/.tflx) を **analyze → decompose → build** の 3 段階で再構築するための AI エージェント補助ツール
- dbt の **staging / intermediate / marts** レイヤ分割と命名規約を Prep に転用する
- dbt 自体は使わない——コンセプトのみ転用

## 設計思想 / 使いどころ

このリポジトリは dbt のレイヤ規律のエッセンスを Prep に転用する道具であり、**本質的には DWH 側でやるべき data modeling を Prep で代替するためのものではない**。

理想形は **DWH 側で staging → intermediate → marts まで構築済み**、Tableau には mart 以降だけが Published Data Source として連携されている状態。Prep の出力は基本的に Hyper Extract として全行マテリアライズされるため、**DWH 側で View / Materialized View / dbt model としてやれるなら常にそちらが安い**（ストレージ・再計算コスト・lineage 可視性すべての観点で）。

このリポジトリの Skill を使うのは、以下のいずれかに該当する場合に限る:

- DWH 側で modeling を組む権限・組織体制が無く、Prep に押し込まざるを得ない
- 既に長大化した Prep フローが存在し、それを段階的に正気に保ちたい
- Python / R ステップ、Prep 固有のピボット処理など、本当に Prep にしかできない処理が含まれる

### Mart 層は fct / dim / rpt の三本立て

dbt のベストプラクティスを Prep に転用するが、Tableau Workbook には **Published Data Source 同士を Relationship / Join できない** という制約がある（結合は Data Blending のみで、非加法集計に弱い）。そのため mart 層は次の三本立てで構成する:

| 種別 | 役割 |
|---|---|
| `fct_<entity>.tfl` → Published DS | 1 ファクト 1 ファイル。再利用可能な素材 |
| `dim_<entity>.tfl` → Published DS | 1 ディメンション 1 ファイル。複数 fct で共有 |
| `rpt_<scope>.tfl` → Published DS | fct × dim を Prep 内で物理 JOIN した OBT。BI が読む完成品 |

- 軽い 1 メトリック重ね合わせ程度なら fct/dim 直読 + Data Blending で済む
- 複数 dim 込みの本格分析が必要なら rpt_*.tfl を作って結合済み Published DS を提供する
- 事前集計が必要な場合は `agg_<entity>_<grain>` (例: `agg_revenue_monthly`)。粒度を明示し、**atomic な fct から再計算可能**であることを保つ

本来は DWH 側で modeling するのがコスト面で最適。本リポジトリの Skill は「DWH 側でやれない」前提で Prep に押し込む場合の補助である点を忘れない。

### スコープ — 何をしない Skill か

本 Skill は **Prep 内で完結させる前提** で動く。具体的に、以下は **意図してスコープ外**:

- DWH 側への push-down 提案（DB View 化・仮想接続定義・DS Calculated Field 化）の自動判定・候補出力
- 既存 Prep ロジックを「DWH 側で書き直すべきか」のレイヤ別判定

理由: DWH 側で modeling を組める組織なら、本 Skill を使う前段でそちらに寄せているはず。逆に Skill を使う状況（上記利用条件）では DWH 側を触れない / 触らない前提が成立している。したがって push-down 検討は **Skill 利用前にユーザー側の組織判断として済んでいる** ものとして扱い、Skill の出力にはこの軸の提案は載らない。

## 含まれる Skill / Workflow

このリポジトリは 4 つの Skill (`prep-extractor` / `prep-architect` / `prep-builder` / `prep-deployer`) で構成される。Workflow 全体図、各 Skill の役割と副作用区分、起動順序は [CLAUDE.md](CLAUDE.md#workflow) に集約してある（Agent 起動時に自動ロードされる真の source）。

## 既知の制限

### Cloud 上で flow のプレビュー画像が出ない ("Flow image unavailable")

本 Skill が生成して publish した flow は Tableau Cloud の Overview / プロジェクト一覧で `Flow image unavailable` のままになる (DAG プレビュー画像が表示されない)。

- 原因: tableauserverclient の `server.flows.publish()` 経由の publish では Cloud 側で flow image が生成されない既知バグ ([server-client-python #1537](https://github.com/tableau/server-client-python/issues/1537), 2024-11 報告、現在 open)
- 回避策の調査結果:
  - Web Authoring (Cloud 上で "Edit Flow" → 保存) → 効果なし
  - 元 .tfl の `flowGraphImage.png` / `flowGraphThumbnail.svg` を generated .tfl に同梱して再 publish → 画像は表示されるが **元 flow の DAG = 分解前の全体絵** が表示され実態と乖離 (誤誘導リスクあり、採用しない)
  - Tableau REST API に flow thumbnail を GET / 再生成する endpoint は存在しない ([Flow Methods](https://help.tableau.com/current/api/rest_api/en-us/REST/rest_api_ref_flow.htm))
- 確実に正しい画像を出す唯一の手段: Tableau Prep Builder Desktop で各 .tfl を開いて save → そこから手動 publish (Builder 経由の publish はバグ対象外)
- Skill のスコープ: 自動で画像生成・同梱はしない。表示が必要なら手動 Builder publish を推奨

## 認証

REST API への認証は Personal Access Token (PAT) を `.env` ファイル経由で渡す。テンプレは [.env.template](.env.template) を参照。実 `.env` は `.gitignore` で除外。

## 使い方

1. このリポジトリを Claude Code が認識できる場所に配置（または plugin として配布）
2. 自分の Prep プロジェクトで Claude Code を起動
3. 既存 .tfl を指して `/prep-architect` を呼び出す（フェーズの一部だけ実行することも可能）
4. 出力された新 .tfl を Prep Builder で開いて検証

詳細な前提（命名規約・推奨フォルダ構造）は [CLAUDE.md](CLAUDE.md) を参照。

## ライセンス

[MIT License](LICENSE)
