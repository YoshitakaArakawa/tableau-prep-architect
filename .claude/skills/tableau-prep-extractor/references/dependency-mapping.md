---
purpose: tableau-prep-extractor Phase C (flow dependency mapping) の実行手順と出力仕様
note: map_flow_dependencies.py の CLI と flow-dependencies.md のセクション構成・消費者を規定。複数フロー移行の計画時のみ使う
---

# dependency-mapping (Phase C)

フロー間の依存 (A の入力 PDS = B の出力 PDS) を機械抽出して着手順を確定する。フロー名やドメイン直感から着手順を推定すると外れる (例: "stats" フローが "incremental" フローの出力を消費する逆向き依存) ため、必ず本フェーズの出力で確定させる。

## いつ使うか

- 複数フロー移行プロジェクトの計画文書を書くとき (移行順の根拠)
- 着手順・passthrough の暫定/恒久判定に疑義が出たとき
- **毎セッションでは走らせない**: 依存マップはプロジェクト内で安定。フロー集合が変わった時のみ再生成

## 実行

```bash
# ローカル: flow.json / .tfl 群 (ファイル or ディレクトリ) を指定
python scripts/map_flow_dependencies.py <flow files/dirs...> -o <work>/reports/flow-dependencies.md

# サーバー: プロジェクト内の全フローを一括 DL してから解析
python scripts/map_flow_dependencies.py --project "<project>" --download-dir <work>/scratch/flows \
    -o <work>/reports/flow-dependencies.md
```

移行計画 (tableau-migration-planner) を作る場合は `--json <work>/reports/flow-dependencies.json` も付けて raw facts (incremental 列込み) を出力する (`init_plan.py` が消費する)。

## 出力 (`flow-dependencies.md`)

| セクション | 内容 | 消費者 |
|---|---|---|
| Per-flow inputs / outputs | フロー別の出力 PDS / 入力 (pds・vconn・other) / incremental (append 有無・control field、backfill 候補判定用) | tableau-migration-planner・architect |
| In-scope dependency edges | consumer → producer エッジ (= 暫定 passthrough 対象の特定) | tableau-migration-planner・architect (self-check 項目 15) |
| Topological migration order | producer 先行の着手順 | tableau-migration-planner |
| Shared vconn tables | 同一 vconn テーブルを 2+ フローが読む一覧 (= stg 再利用候補) | architect (self-check 項目 15) |
| Warnings | 同名出力 PDS の曖昧エッジ / 循環 | 全員 |
