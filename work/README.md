# work/

このフォルダは移行セッションの **公式成果物置き場**。「スクラッチ (使い捨ての遊び場)」ではなく、セッションの全成果物 (Skill 出力 / .tfl / build スクリプト) をここに集約する。使い捨ての試行錯誤は各セッションの `scratch/` サブフォルダに限定する。**この `README.md` 以外は git で追跡しません**（`.gitignore` で除外済み）。

## 想定する用途

- 実 .tfl/.tflx を Tableau Cloud / Server から DL して analyze する
- 分析レポート（analysis-*.md）を保存
- 分解設計案（decomposition-plan-*.md）を保存
- 生成した .tfl 群の動作確認用一時置き場
- 試行錯誤のスクラッチ (各セッションの `scratch/` 配下に限定)

## 命名規約とサブフォルダ構造

各作業セッションごとに **日付プレフィクス＋作業内容サマリー** のサブフォルダを切る。直下は **入力 (.tfl, flow.json) + 4 サブフォルダ (reports/ flows/ scripts/ scratch/)** で固定:

```
work/
├── 20260515_legacy-flow-analysis/
│   ├── downloaded.tflx               # 入力
│   ├── flow.json                     # 入力 (展開済)
│   ├── reports/                      # Skill 生成 MD/JSON
│   ├── flows/                        # prep-builder 出力 .tfl
│   ├── scripts/                      # 公式の再生成スクリプト
│   └── scratch/                      # 試行錯誤・使い捨て
├── 20260520_int-step-split-experiment/
│   └── ...
└── 20260601_rpt-design-experiment/
    └── ...
```

形式：`work/YYYYMMDD_<作業内容>/`

- 日付は **作業開始日**
- 作業内容は短く（snake_case / kebab-case どちらでも可、可読性優先）
- Skill ごとにフォルダを切るのではなく、**ファイルの「役割」で分離** する (Skill が増えても直下が膨張しない)

## サブフォルダの責務

| サブフォルダ | 入れるもの | 入れないもの |
|---|---|---|
| `reports/` | prep-extractor の `flow-summary.md` / `deploy-context.md` / `flow-dependencies.md`、prep-migration-planner の `migration-plan.md` / `migration-plan.json`、prep-architect の `analysis-*.md` / `decomposition-plan-*.md`、prep-builder/deployer の `publish-manifest.json`、prep-output-comparator の `comparison-report.md` / `pairs.json` | スクリプト、.tfl |
| `flows/` | prep-builder の `staging/*.tfl` / `intermediate/*.tfl` / `marts/*.tfl` | レポート、試行錯誤の .tfl |
| `scripts/` | **公式の再生成スクリプト** (例: `build_tfls.py` — このセッションの .tfl 群を再ビルドできるもの)。冪等で再実行可能 | 1 回限りの修正試行・実験 |
| `scratch/` | 試行錯誤・使い捨ての py / メモ (例: `patch_target_path.py`, 検証用 `regression_test_*.py`) | 後段の Skill が依存するスクリプト |

迷ったら: 機械生成 MD/JSON → `reports/` / .tfl 成果物 → `flows/` / 再ビルド時に再実行する公式 → `scripts/` / その他 → `scratch/`。

## 実行時間の事後計測 tip

セッションの phase 別所要時間が知りたくなったら、`work/<session>/` 配下のファイル mtime を時系列で並べると粗い timeline を復元できる。各 phase は特徴的なファイルを残す (`flow-summary.md` = Phase A 完了 / `deploy-context.md` = Phase B 完了 / `analysis-*.md`・`decomposition-plan-*.md` = architect / `flows/*/*.tfl` = builder / `comparison-report.md` = comparator)。PowerShell:

```powershell
Get-ChildItem work/<session> -Recurse -File | Sort-Object LastWriteTime | Select-Object LastWriteTime,FullName
```

subagent fork の内部時間が見えないとき特に有用 (fork 内の breakdown は [references/skill-timing-contract.md](../references/skill-timing-contract.md) の Timing ブロックが一次情報)。

## 昇格ルール

`work/` で試して固まった内容は、適切な場所に **昇格** させる：

- 設計判断・思想 → [README.md](../README.md) の `## 設計思想 / 使いどころ` 節
- 規約・判断基準 → `CLAUDE.md` または該当 Skill の `references/`
- 実装ロジック → 該当 Skill の `scripts/`
- 動作確認できた .tfl → prep-deployer で Tableau Cloud へ publish (サーバー側が正本)

## 注意

- このフォルダの中身は **公開リポジトリには出ない** 前提（`.gitignore` で除外）
- 機密性のある会社固有名・実 DB ホスト・実ユーザーデータを置いても良い
- ただし将来 `git add -f` で誤って強制追加しないよう注意
- 作業終了後、不要なら **削除して良い**（特に DL した .tfl 等の大きいファイル）
