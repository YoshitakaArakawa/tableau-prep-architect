# work/

このフォルダはこのリポジトリでの **実験・検証・分析の作業フォルダ**。**この `README.md` 以外は git で追跡しません**（`.gitignore` で除外済み）。

## 想定する用途

- 実 .tfl/.tflx を Tableau Cloud / Server から DL して analyze する
- 分析レポート（analysis-*.md）を保存
- 分解設計案（decomposition-plan-*.md）を保存
- 生成した .tfl 群の動作確認用一時置き場
- 試行錯誤のスクラッチ

## 命名規約

各作業セッションごとに **日付プレフィクス＋作業内容サマリー** のサブフォルダを切る：

```
work/
├── 20260515_legacy-flow-analysis/
│   ├── downloaded.tflx
│   ├── analysis.md
│   └── decomposition-plan.md
├── 20260520_int-step-split-experiment/
│   └── ...
└── 20260601_rpt-design-experiment/
    └── ...
```

形式：`work/YYYYMMDD_<作業内容>/`

- 日付は **作業開始日**
- 作業内容は短く（snake_case / kebab-case どちらでも可、可読性優先）

## 何を置く / 置かない

| 置く | 置かない |
|---|---|
| DL した .tfl/.tflx（テスト対象） | 本番運用する .tfl（→ ユーザー作業フォルダの `flows/` へ） |
| 分析レポート・分解設計案（markdown） | 公開すべき規約・判断基準（→ Skill の `references/` へ昇格） |
| 試行錯誤のスクラッチ | 実装に組み込んだロジック（→ `scripts/` へ昇格） |
| 実データのサンプル（小規模） | 大量データ・機密実データ（別の安全な場所へ） |

## 昇格ルール

`work/` で試して固まった内容は、適切な場所に **昇格** させる：

- 設計判断・思想 → [README.md](../README.md) の `## 設計思想 / 使いどころ` 節
- 規約・判断基準 → `CLAUDE.md` または該当 Skill の `references/`
- 実装ロジック → 該当 Skill の `scripts/`
- 動作確認できた .tfl → ユーザーの本番作業フォルダの `flows/`

## 注意

- このフォルダの中身は **公開リポジトリには出ない** 前提（`.gitignore` で除外）
- 機密性のある会社固有名・実 DB ホスト・実ユーザーデータを置いても良い
- ただし将来 `git add -f` で誤って強制追加しないよう注意
- 作業終了後、不要なら **削除して良い**（特に DL した .tfl 等の大きいファイル）
