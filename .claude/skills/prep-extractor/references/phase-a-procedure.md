# Phase A 実装手順

Phase A (flow extraction) の 7 ステップ実装ガイド。SKILL.md からは 1 行サマリでしか参照されないため、本ファイルに詳細を集約する。

## Step 1: 入力ファイルの展開

`.tfl` / `.tflx` の場合は Repo 直下 [scripts/flow_io.py](../../../../scripts/flow_io.py) の `unpack_flow_json` を使う:

```bash
python -c "
import sys; sys.path.insert(0, 'scripts')
from flow_io import unpack_flow_json
unpack_flow_json('<input.tfl>', 'work/<date>/flow.json')
"
```

## Step 2: 構造情報の確認

以下を Read して flow JSON の構造を把握する:

- [../../../../references/tfl-json-schema.md](../../../../references/tfl-json-schema.md) — ファイル形式、トップレベル JSON 構造、UI ステップ ⇔ nodeType ⇔ actions サブタイプの対応表、`previousNodes` の罠、`beforeActionAnnotations` ラップ構造、`initialNodes` BFS 規約
- [flow-summary-format.md](flow-summary-format.md) — 出力書式の厳密仕様

## Step 3: トポロジ復元

LLM 自身が flow.json を読んで以下を抽出する（ノード数が多ければ Bash で支援してよい）:

1. `flow["initialNodes"]` をエントリーポイントとして BFS 順序を確定 → 短 ID `#1, #2, ...` を採番
2. 各ノードの `nextNodes[].nextNodeId` を抽出 → これを反転して各ノードの `Prev` を求める（`previousNodes` は空なので使わない）
3. `nodeType` の version prefix を剥がす（最後のドット以降を採用）
4. ノード名（`name`）と組み合わせて Topology テーブルを構築

BFS の擬似コードは [tfl-json-schema.md](../../../../references/tfl-json-schema.md) 参照。

## Step 4: actions inventory 生成

各 SuperTransform について `beforeActionAnnotations` 配列を走査:

- 各要素は `{"annotationNode": {...}}` でラップされているので 1 階下ろす
- 内側 `nodeType` の末尾に応じて要約フォーマットを選ぶ
- 詳細フォーマットは [flow-summary-format.md](flow-summary-format.md) の actions inventory セクション参照

ノード数が多くて手動走査が辛い場合は支援スクリプトを使う:

```bash
python .claude/skills/prep-extractor/scripts/inspect_actions.py \
    work/<date>/flow.json \
    -o work/<date>/scratch/_actions-tmp.md
```

このスクリプトの出力はそのまま flow-summary.md の対応セクションに転記してよい。Topology テーブルや Mermaid DAG は LLM が直接生成する。

## Step 5: Mermaid DAG 生成

Topology の Next 列をベースに `graph TD` を出力。各ノードは `n<id>[#<id> <nodeType> <Name>]` 形式。分岐・合流が一目で分かるレイアウトを保つ。

## Step 6: Warnings 集約

走査中に検出した以下を Warnings セクションに列挙:

- 未知 nodeType
- 未知 action type
- `beforeActionAnnotations` が空 (`0 actions`) の SuperTransform — 削除候補
- 重複名のノード — build 時のファイル名衝突要注意
- 孤立ノード
- 循環依存の兆候
- **SuperUnion ノード — 全件必ず 1 行ずつ追加**: `🔒 Node #N <UnionName> (SuperUnion): injects implicit Table Names column — do NOT propose deletion`。actions=0 や branch 同一性に関わらず Union は schema 等価ではない (`Table Names` を暗黙注入する)。analyze / decompose 側で削除候補と判定するのを構造的に防ぐ二重防御

## Step 7: 書き出しと完了報告

`flow-summary.md` を指定パスに Write。ユーザーには以下を簡潔に報告:

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
