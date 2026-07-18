---
purpose: prep-pulse-repointer の成果物 (inventory / design / runbook / verify report) のフォーマット仕様
note: design.json は repoint / verify モードの機械入力なのでフィールドを厳格に守る。runbook / report は人間向けで、必須セクションを満たせば文面は調整可
---

# Pulse Repoint 出力フォーマット

## 目次
- pulse-repoint-inventory.json (design Step 1、中間)
- pulse-repoint-design.json (design Step 2、repoint / verify の機械入力)
- pulse-repoint-runbook.md (design Step 2、人間向け)
- repoint モードの RESULT_JSON
- pulse-repoint-verify-report.md (verify モード)

## pulse-repoint-inventory.json (design Step 1、中間)

```json
{
  "generated_at": "<ISO8601 JST>",
  "source_project": "<project name>",
  "total_definitions": 12,
  "definitions": [
    {
      "definition_id": "<luid>",
      "name": "<定義名>",
      "datasource_id": "<PDS luid>",
      "datasource_name": "<PDS 名>",
      "datasource_project": "<project 名>",
      "in_scope": true,
      "referenced_fields": ["Close", "Date", "Name"],
      "metrics": [
        {"metric_id": "<luid>", "is_default": false, "followers": [{"user_id": "<luid>"}]}
      ]
    }
  ],
  "errors": []
}
```

- `in_scope` = datasource_project が `source_project` に一致するか
- `referenced_fields` は specification (viz_state の `fieldCaption` / basic_spec の
  `measure.field`・`time_dimension.field`・`filters[].field`) から機械抽出した参照フィールド名
- `followers` は site 全体 subscriptions を metric_id で突合した結果

## pulse-repoint-design.json (design Step 2、repoint / verify の機械入力)

```json
{
  "generated_at": "<ISO8601 JST>",
  "source_project": "<project name>",
  "pairs": [
    {
      "definition_id": "<旧定義 luid>",
      "definition_name": "<定義名>",
      "old_pds": {"luid": "<luid>", "name": "<名>"},
      "new_pds": {"luid": "<luid>", "name": "<名>", "match": "luid"},
      "referenced_fields": ["..."],
      "followers_total": 2,
      "migration_scope": "followed",
      "non_default_metrics": [
        {"metric_id": "<luid>", "specification": {"...": "..."}, "followers": [{"user_id": "<luid>"}]}
      ],
      "default_metric_followers": [{"user_id": "<luid>"}]
    }
  ],
  "unmapped_old_pds": [{"luid": "<luid>", "name": "<名>", "definitions": ["<定義名>"]}],
  "out_of_scope": [{"definition_name": "<名>", "datasource_name": "<名>", "project": "<名>"}]
}
```

- `new_pds.match`: `"luid"` (manifest `original.outputs[].luid` 一致) / `"name"` (名前 fallback)
- `migration_scope`: `"followed"` (follower あり = 移行対象) / `"unfollowed"` (follower なし =
  破棄候補。既定では repoint せずカットオーバー時に旧定義ごと削除。ユーザーは repoint モードに
  定義 id を渡すだけで昇格できる)。design 時点のスナップショット — production はライブ読み、
  verify が破棄候補の後発 follower を監視する
- `non_default_metrics` / `default_metric_followers` は design 時点のスナップショット (runbook の
  impact 表示用)。production の移行入力は**実行時のライブ読み**であってこの値ではない

## pulse-repoint-runbook.md (design Step 2、人間向け = go/no-go 判断書)

ユーザーがこの 1 枚で「今回の移行で影響を受ける Pulse 資産の全量・引き継がれないもの・
残余リスク」を確認し、repoint の go/no-go を判断できることが目的。必須セクション:

1. **Impact (対象一覧、follower 有無で 2 階層)**: 定義ごとに 1 行 — 定義名 / 旧定義 id /
   旧 PDS → 新 PDS / 再作成される scoped metric 数 / follower (数 + id) / カットオーバーで
   消える範囲 (旧定義 + metric + 購読)。**Impact 1 = follower あり (移行対象)**、
   **Impact 2 = follower なし (破棄候補 — 既定は repoint せず削除、昇格可、follower ゼロは
   完全な未使用の保証ではない旨を明記)**。copy-promote で**定義 id が変わる**ことを冒頭に明記
   (新 id は production 後に verify レポートで確定)。参照フィールドの一覧も添える
2. **引き継がれないもの**: 定義/metric id (ブックマーク・埋め込みの張り直し)、
   insight 履歴・digest 学習のリセット、猶予期間中の新旧二重表示
3. **対象外**: raw 等スコープ外 PDS 参照の定義と理由
4. **段取り**: rehearsal → 承認 → production (rehearsal コピー昇格 + **ライブ follower 読み**) →
   verify → 旧定義 (`(pre-repoint)`) の人間削除。旧定義削除で metrics + subscriptions が
   連鎖削除されることを明記
5. **残余リスク (注意喚起、固定文)**: (a) Desktop / Web authoring / API での旧 PDS 直接利用は
   列挙不能 — 旧 PDS 削除前に周知期間を置く、(b) 新 PDS の閲覧権限はユーザー責務
6. **カットオーバー前チェックリスト**: verify PASS / follower 再購読済み / 残余リスク 2 点の確認
7. **unmapped / warnings**: 対応先が確定できなかった旧 PDS と対処 (manifest 追加渡し等)

## repoint モードの RESULT_JSON

rehearsal:

```json
{"stage": "rehearsal", "results": [
  {"definition_id": "<旧>", "rehearsal_id": "<コピー>", "verdict": "match",
   "original_markup": "...", "rehearsal_markup": "..."}
], "elapsed_s": 12.3}
```

- `verdict`: `match` (markup の数値部一致) / `differs` (両方 201 だが値が違う) /
  `probe_failed` (コピー側 insight 非 201)

production:

```json
{"stage": "production", "results": [
  {"definition_id": "<旧>", "new_definition_id": "<新>",
   "renamed_old_to": "<元名> (pre-repoint)",
   "created_via": "promoted_rehearsal",
   "metrics_migrated": 2, "subscriptions_migrated": 1,
   "insight_verdict": "ok", "rehearsal_deleted": false}
], "elapsed_s": 45.6}
```

- `created_via`: `promoted_rehearsal` (rehearsal コピーを rename 昇格 — 通常経路) / `created`
  (rehearsal 未実施で新規作成) / `existing` (再実行で既に昇格済み)
- `metrics_migrated` / `subscriptions_migrated` は**実行時に旧定義から読み直したライブ状態**の
  移行数 (design スナップショットではない)

部分失敗時は `results[].error` にステップ名 + メッセージを入れ、完了済みステップを
残して返す (再実行は getOrCreate / 既存購読スキップで冪等)。

## pulse-repoint-verify-report.md (verify モード)

必須セクション:

1. **overall_verdict**: `PASS` / `INCOMPLETE` / `EMPTY`
2. **per-definition 表**: 元名の定義が新 PDS 参照か / follower 数 (新定義の実測 vs
   旧定義 `(pre-repoint)` の**ライブ**購読数。旧定義が削除済みなら期待値 `-` でスキップ) /
   insight probe 結果
3. **破棄候補**: `migration_scope=unfollowed` で未昇格の定義 (判定対象外)。旧定義のライブ購読に
   **後発 follower が現れたら ⚠️ 警告** (未使用前提が崩れた — 昇格を検討)
4. **残存 warning**: `(pre-repoint)` 旧定義・rehearsal コピーの残存一覧 (削除は人間判断)
5. **要対応**: FAIL / 不足の定義と推奨アクション (後発 follower 警告を含む)
