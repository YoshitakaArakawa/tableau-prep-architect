---
purpose: design モードの 2 出力 (schedule-setup-runbook.md / schedule-design.json) のフォーマット規約
note: runbook は人間が Cloud UI で Linked Task を再現するための資料、design JSON は verify モード (verify_schedules.py) が機械突合に使う構造化版。両者は同じ設計の 2 表現で、内容の食い違いは design モードのバグ
---

# Runbook / Design JSON フォーマット

## 目次
- schedule-setup-runbook.md（人間向け）
- schedule-design.json（機械向け）
- 記載粒度の原則

## schedule-setup-runbook.md（人間向け）

必ずこの構造を使う。「この 1 枚だけで UI 再現できる」ことが受け入れ条件。

````markdown
---
title: <対象> スケジュール設計 (Linked Task セットアップ図)
created_at: <yyyymmdd>
scope: <一文。API 作成をしない旨を含める>
source_of_truth: schedule-inputs.json (<パス>) + ユーザー確認済みトリガ方針
site: server <URL> / site <site名>
---

# <タイトル>

<冒頭 1 段落: 対象は int/mart のみ、stg は Live PDS で不要、LUID/run-type/順序を省略しない旨>

## UI での作り方 (全ドメイン共通、5〜7 ステップ)
1. 1 行目のフローを開く → [Linked Tasks] タブ → 新規作成
2. 表の実行順どおりにメンバーを追加（先行依存が同じ行同士は並列可）
3. run-type=Incremental の行だけ「増分更新」を指定（間違えると append 重複）
4. トリガ設定（曜日 + 時刻。サイトタイムゾーン基準）
5. 保存 → メンバー順・run-type・曜日を目視確認

### 並び順の基本方針: facts-last
<topological sort + leaf mart 末尾集約 + hub mart は consumer より前固定、の 3 行>

## 現状確認 (probe 実測、読み取りのみ)
<新フローへの既存スケジュール有無 / 旧タスクの state / stale look-alike 警告>

## Linked Task <X> — <ドメイン名>
- **トリガ**: <毎日 or 平日 Mon–Fri> **HH:MM JST**（= HH:MM UTC）（元 <schedule id> 踏襲/変更理由）
- **1 行目フロー**: <名前>
- <依存構造・UI 注意（stale がある場合はここに）>

| 実行順 | flow 名 | flow LUID | layer | run-type | 先行依存 | UI URL |
|---|---|---|---|---|---|---|
| 1 | ... | `<LUID>` | intermediate | **Incremental** (control: <field>) | — | <数値> |
| 2 | ... | `<LUID>` | marts | Full (int を mirror, **hub=固定**) | 1 | <数値> |

- **⚠️ run-type**: Incremental は実行順 <n> のみ（根拠: .tfl 実測）。誤って Full で回すと append 重複、の警告

## Incremental な行の一覧
| ドメイン | 実行順 | flow | layer | control field |
<全ドメイン横断の表 + 「これ以外は全て Full」>

## 末尾: 旧スケジュールの残置と削除タイミング
<削除対象 id 一覧（Linked Task + standalone を両方）と削除の発火条件>
````

## schedule-design.json（機械向け）

verify_schedules.py の入力。runbook と同時に design モードが書く。

```json
{
  "schema_version": "1",
  "created_at": "<yyyymmdd>",
  "domains": [
    {
      "name": "A — <ドメイン名>",
      "trigger": {
        "frequency": "Daily",
        "time_utc": "00:00",
        "time_jst": "09:00",
        "weekdays": ["Mon", "Tue", "Wed", "Thu", "Fri"]
      },
      "steps": [
        {
          "order": 1,
          "flow_name": "<name>",
          "flow_luid": "<LUID>",
          "layer": "intermediate",
          "run_type": "incremental",
          "control_field": "<field>"
        }
      ]
    }
  ],
  "old_schedules_to_remove": [
    { "id": "<schedule LUID>", "name": "<schedule 名>", "note": "<旧ドメイン>" }
  ]
}
```

制約:

- `weekdays` は `["Mon", ...]`（3 文字英語表記）または文字列 `"every_day"`
- `run_type` は `"incremental"` / `"full"` のみ。値は **schedule-inputs.json の `run_type` をそのまま転記**する（手で書き換えない — 書き換えが必要なら collect の入力が間違っている）
- `flow_luid` も schedule-inputs.json から転記。runbook 側の表と 1:1 で一致させる

## 記載粒度の原則

- **LUID・run-type・実行順・control field を省略しない**（要約や「同上」で潰さない）
- run-type の根拠は常に「.tfl 実測」と明記する（設計文書からの転記は禁止 — [scheduling-model.md](scheduling-model.md)）
- 時刻は JST/UTC 併記、日付は yyyymmdd
- stale look-alike が probe で出た場合、該当ドメインの表に UI URL 列を必ず付け、判別方法を注記する
