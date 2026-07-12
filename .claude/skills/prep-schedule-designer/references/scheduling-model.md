---
purpose: 分解後 Prep フロー群のスケジュール設計規範。Linked Task の UI/API 制約、run-type 意味論、実行順ポリシー (facts-last)、トリガ設計、検証の限界を規定する
fetched_at: 2026-07-12
note: Tableau Cloud の REST 挙動 (tasks/linked のフィールド形、曜日・run-type の不可視性) はサイト実測に基づく。API 仕様変更時は probe_flow_schedules.py の docstring と併せて更新する
---

# Scheduling Model（設計規範）

## 目次
- スケジュール対象の選別
- Linked Task の制約（UI/API）
- run-type 意味論【最重要】
- 実行順ポリシー: facts-last
- ドメイン分割とトリガ設計
- stale フロー対策
- 旧スケジュールの扱い
- 検証の限界と挙動ベース検証

## スケジュール対象の選別

- **対象 = int / mart の .tfl フローのみ**。stg (kind=pds_augment) は Live PDS で run を持たない → スケジュール不要、依存グラフからも除外（mart 実行時に常に最新ソースを読む）
- 対象フローの正式名・LUID は **session manifest から取得**する（推定・手書き転記をしない）。collect_schedule_inputs.py が集約する

## Linked Task の制約（UI/API）

- **Linked Task は REST で作成・変更できない**（type=System。Cloud UI 専用）。依存順を持つチェーンは人間が UI でセットアップする。本 Skill の成果物はその設計資料と事後検証
- **読み取りは `GET /sites/{site}/tasks/linked`**。リソース名は `linked`（`linkedTasks` は 404）。メンバー順 (stepNumber)・flow LUID・schedule state/frequency/nextRunAt が読める
- **UI のフロー選択ピッカーは「チェーン内フローの downstream」しか候補に出さない**。依存 (PDS lineage) が繋がらないフローは追加できない。設計順が lineage を満たさないと UI で組めない
- **曜日設定 (frequencyDetails) は外部 REST に露出しない**。`frequency: Daily` + `nextRunAt` のみ。`nextRunAt` の曜日から every-day か否かを傍証できるだけ
- **ステップ単位の run-type (Full/Incremental) はどの REST レスポンスにも出ない**（tasks/runFlow 詳細・tasks/linked とも）。→「検証の限界」参照
- standalone（単独フロー）スケジュールのみ REST で作成可能だが、分解後パイプラインは int→mart の順序制約を必ず持つため standalone で組める単位は基本無い

## run-type 意味論【最重要】

- **incremental accumulator（IncrementalConfiguration + append 出力を持つフロー）を Full で回すと、append 出力に現行スナップショットが丸ごと再追記され重複する**。スケジュールで当該フローに必ず「増分更新 (Incremental refresh)」を指定する
- **run-type は設計文書から転記しない**。decomposition-plan や移行計画書の記載は drift しうる。必ず decomposed .tfl の実体（`incrementalEnabled` / `controlFieldName` / `outputOperationTypeAppend`）から機械確定する（collect_schedule_inputs.py → flow_io.get_incremental_config）
- **accumulator の層は一定しない**: int の場合（下流 mart は int を full mirror）も、mart の場合（chain に int が無い）もある。フロー名や層で推定せず出力単位で確定する
- `incrementalEnabled: true` でも `controlFieldName` / `outputNodeId` が空なら **inert（UI 残骸）**。Prep は無視するので Full 扱い。collect が警告として報告する
- 重複を起こした場合の是正: PDS 削除 → baseline full run → 以後 incremental（prep-deployer の run 規律参照）

## 実行順ポリシー: facts-last

PDS lineage の **topological sort** を満たした上で:

1. **int と hub mart（他フローが出力 PDS を読む mart）を依存が許す限り前方に**置く
2. **leaf mart（誰にも読まれない末端 mart）を末尾に集約**する
3. **hub mart は末尾に回せない** — consumer より前に固定（UI ピッカーも consumer の先行を強制する）

依存エッジは .tfl の LoadSqlProxy 入力の `datasourceName` を、他フローの出力 PDS 名と突合して機械抽出する（設計文書からの転記は誤依存の混入源）。collect_schedule_inputs.py がこの順序を `suggested_order` として出力する。

## ドメイン分割とトリガ設計

- **ドメイン = 1 Linked Task = 1 トリガ**。collect が出力する連結成分（components）が依存上の最小単位。相互依存の無い成分を業務ドメインとして 1 Linked Task に束ねるか（トリガ 1 本化で運用が楽）は caller / ユーザーの判断
- **トリガの既定は「元スケジュールの踏襲 + 毎日」**。曜日限定（例: 市場データの平日 Mon–Fri）は**ユーザー指定制** — データの業務特性の判断なので Skill は自動判定しない。caller が intake で確認して fork に渡す
- 週末にソース更新が無いドメインを毎日回しても incremental は 0 行 no-op で壊れない（曜日限定は無駄 run 削減の最適化であり、正しさの要件ではない）
- 資料には JST / UTC を併記する（Cloud UI はサイトのタイムゾーン基準）

## stale フロー対策

同名・類似名の残骸フロー（過去世代のパイロット等）がサーバーに残っていると、**UI ピッカーで誤選択して downstream が繋がらなくなる**。対策:

- probe_flow_schedules.py の look-alike 検出（同名別 LUID / 同層 prefix + 同末尾トークン）で事前に警告
- runbook の表に **flow LUID と UI URL（webpage_url 末尾の数値）を必ず併記**し、人間が UI で判別できるようにする
- 残骸の削除は本 Skill のスコープ外（破棄操作）。別タスクとしてユーザーに提案する

## 旧スケジュールの扱い

- probe で **Linked Task（Suspended 含む）と standalone の全 runFlow タスクを列挙**する。Suspended の Linked Task だけ見て **Active の standalone を見落とすと旧パイプラインが新と二重に走り続ける**
- 旧スケジュールの削除は人間の作業（cut-over 完了後）。runbook 末尾に「削除対象 id 一覧 + 削除タイミング」を必ず載せる
- 新フローに既存タスクが無いこと（衝突ゼロ）を設計前に probe で確認する

## 検証の限界と挙動ベース検証

Phase C（verify_schedules.py）で機械突合できるもの / できないもの:

| 項目 | 機械検証 | 手段 |
|---|---|---|
| Linked Task の存在・メンバー集合 | ✅ | tasks/linked の flow LUID 突合 |
| メンバー順序 | ✅ | stepNumber vs 設計 order |
| state / frequency / トリガ時刻 | ✅ | schedule の state / frequency / nextRunAt の時刻部 |
| 曜日 | △ 部分 | nextRunAt の曜日が設計 weekdays 内かのみ（REST が設定を隠すため） |
| 各ステップの run-type | ❌ | REST 不可視。**挙動ベース検証**に切替（下記） |
| 二重発火（standalone 併存 / 複数 Linked Task 所属） | ✅ | runFlow タスクと linked メンバーの突合 |

**run-type の挙動ベース検証**: 初回スケジュール実行の完了後、incremental フローの append 出力について control field の期間内行数を実行前と比較する。**二重化していれば Full で走った**証拠 → UI で run-type を修正し、出力を是正（PDS 削除 → baseline full → 以後 incremental）。verify レポートがこのチェックリストを自動生成する。
