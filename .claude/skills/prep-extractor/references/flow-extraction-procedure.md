# Phase A 実装手順

Phase A (flow extraction) の 4 ステップ実装ガイド。SKILL.md からは 1 行サマリでしか参照されないため、本ファイルに詳細を集約する。

## Step 1: 入力ファイルの展開

`.tfl` / `.tflx` の場合は Repo 直下 [scripts/flow_io.py](../../../../scripts/flow_io.py) の `unpack_flow_json` を使う:

```bash
python -c "
import sys; sys.path.insert(0, 'scripts')
from flow_io import unpack_flow_json
unpack_flow_json('<input.tfl>', 'work/<date>/flow.json')
"
```

## Step 2: flow-summary.md の機械生成

`gen_flow_summary.py` を **実行** する。5 セクション (Meta / Topology / Dependency DAG / SuperTransform actions inventory / Warnings) すべてをこのスクリプトが生成する。セクションを手で組み立てない（部分的な summary を防ぐため低自由度に固定）:

```bash
python .claude/skills/prep-extractor/scripts/gen_flow_summary.py \
    work/<date>/flow.json \
    -o work/<date>/reports/flow-summary.md \
    --flow-name "<元 .tfl のファイル名 stem>"
```

flow JSON の構造を深掘りしたい場合の参照:

- [../../../../references/tfl-json-schema.md](../../../../references/tfl-json-schema.md) — ファイル形式、UI ステップ ⇔ nodeType ⇔ actions の対応、`previousNodes` の罠、`initialNodes` BFS 規約
- [flow-summary-format.md](flow-summary-format.md) — 出力書式の厳密仕様（スクリプトが実装している）
- actions 部分だけ欲しいときは `inspect_actions.py`（`gen_flow_summary.py` が内部で import している要約ロジックの単体版）

## Step 3: 生成結果のレビュー

生成された flow-summary.md を Read して検算する:

1. **5 セクションが揃っているか**（欠けていたら Step 2 のコマンド失敗を疑う。手で補筆しない）
2. Topology の Prev/Next に明らかな断絶がないか（Disconnected 警告と突き合わせ）
3. Warnings の ⚠️（未知 nodeType / action type）が出ていたら、処理は続行しつつ完了報告に含める

## Step 4: 書き出しと完了報告

ユーザーには以下を簡潔に報告:

- 出力パス
- 総ノード数 / SuperTransform 数 / 総 actions 数
- Warnings の件数（重要なものは 1-2 件抜粋）
- 次に推奨するアクション（例: 「prep-architect の analyze フェーズへ」）

## エラーハンドリング

| エラー | 挙動 |
|---|---|
| 入力ファイルが見つからない | 中断、パス確認をユーザーに依頼 |
| zip 展開失敗 (`.tfl` / `.tflx`) | 中断、ファイル破損の可能性を報告 |
| `flow["nodes"]` がない / 空 | 中断、Prep フローの JSON ではない可能性 |
| `initialNodes` が空 | Warnings に記載しつつ、ノード辞書の全件を topological 推定でフォールバック |
| 未知 nodeType / action type | **中断しない**、Warnings に記載してそのまま処理続行 |
| 出力先ディレクトリが存在しない | 親ディレクトリを `mkdir -p` で作成 |

## サーバーからの DL（任意のサブ手順）

```bash
# LUID 確認（URL の数値 ID は LUID ではない）
python .claude/skills/prep-extractor/scripts/list_flows.py --name-contains "<flow name>"

# DL
python .claude/skills/prep-extractor/scripts/download_flow.py \
    --flow-id <LUID> \
    --output work/<date>/source.tflx
```

認証は Repo 直下 [scripts/tableau_auth.py](../../../../scripts/tableau_auth.py) が `.env` を読む。

## URL ID 解決について

`https://<your-pod>.online.tableau.com/#/site/<contentUrl>/projects/<id>` の数値 ID は **vizportalUrlId** で、Tableau REST API の標準エンドポイント (`GET /sites/{site-id}/projects`) には **返らない**。よって `1117306` のような数値から LUID への直接マップは不可。代替手段:

- ユーザーに project name または `Parent/Child` path を聞く（本フェーズの基本動作）
- Metadata API (GraphQL) も `vizportalUrlId` を返さないため逆引き不可（検証済み）

## 制約 (Phase A)

- **Tableau Prep のバージョンに依存**。新バージョンで構造が変わったら Repo 直下 [tfl-json-schema.md](../../../../references/tfl-json-schema.md) を更新する
- **業務的解釈・レイヤ推定はしない**（それは prep-architect analyze の役割）
- **分解設計もしない**（同 decompose の役割）
- 本 Skill は純粋に「構造の機械的抽出 + Mermaid 可視化」に専念する
