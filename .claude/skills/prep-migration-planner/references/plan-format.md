---
purpose: migration-plan.json のスキーマと migration-plan.md テンプレートの仕様
note: init_plan.py が書く JSON の必須/nullable フィールドと、render_migration_plan.py が生成する md の構造を規定する。JSON が正・md はレンダリング。決定/ファクト/status の 3 分類の根拠は orchestration-model.md
---

# Plan Format

## 目次

- 正準置き場 (canonical location)
- migration-plan.json スキーマ
- 必須 / nullable の別
- フィールド詳細
- migration-plan.md テンプレート
- レンダリング規則

## 正準置き場 (canonical location)

`migration-plan.json` は **初版を生成したセッションの `work/<yyyymmdd>_<tag>/reports/`** が正準置き場 (`.md` も同じ directory)。移行はセッションを跨いで進むが、計画書は移動・複製せず初版の場所に置き続け、以後のセッションはそこを読み書きする。

- 新セッションで resume するときは、intake でこのパスを受け取る (**resume 時の必須質問**)。パスが不明なら過去セッションの `work/` を探すより、ユーザーに初版フォルダを確認する
- `pointers.manifests` に各セッションの publish-manifest パスを追記していくことで、1 枚の計画書から全セッションの成果物を辿れる
- セッション横断の resume state (schedule / repoint / backfill の進捗) はこの 1 ファイルが持つ ([orchestration-model.md](orchestration-model.md))

## migration-plan.json スキーマ

status 値は `pending` / `in_progress` / `done` / `fail` / `partial` / `n/a`。

```jsonc
{
  "meta": {
    "target_path": "99_Sandbox/<project>_decompose",   // 必須(init)
    "goal_stage": "⑤ E2E 比較まで",                     // 必須(init) 表示ラベル (goal 整数から導出)
    "flow_count": "multi",                              // 必須 "single"|"multi" 単発分岐判定
    "created_marker": "20260712 14:03:07 JST"           // init 時に datetime で記録 (resume 台帳の生成時刻)
  },
  "scope": {                                            // 必須(init) 決定
    "in_scope": ["<元フロー名>", "..."],
    "out_of_scope": []
  },
  "migration_order": {                                  // 必須(init) ファクト=pointer参照 + 採否は決定
    "order": ["<元フロー名>", "..."],                   // 複数=flow-dependencies の topological、単発=単一
    "source": "flow-dependencies.md"                    // 無ければ null (単発)
  },
  "session_batches": [                                  // multi のみ。single では null
    { "tag": "<yyyymmdd>_<tag>", "flows": ["..."], "reuse": "deploy-context.md" }
  ],
  "backfill_candidates": [                              // 必須(init、空可) 機械提示
    { "flow": "<元フロー名>", "control_field": "Date",
      "applicable": true, "mode": null, "reason": "incremental/append 検出 (control=Date)" }
    // mode: null|"seam"|"replace" は compare 後にユーザー合意で埋める(決定)
  ],
  "trigger_policy": null,                               // nullable(schedule 直前) 決定
  "old_schedule_notes": null,                           // nullable(schedule/cut-over) 決定
  "matrix": {
    "rendered_after": "decompose",
    "rows": []                                          // build 開始時に生成。1 行 = 分解後 .tfl
  },
  "human_queue": [                                      // 必須(init 骨) 決定+段取り
    { "step": 1, "trigger_condition": "各 int/mart publish 後",
      "action": "Linked Task を UI 作成", "status": "pending", "runbook_ref": null }
  ],
  "pointers": {                                         // 必須(init) ファクトの出所
    "flow_dependencies": "flow-dependencies.md",
    "deploy_context": "deploy-context.md",
    "manifests": []                                     // resume で追記
  },
  "status_note": "status fields are a re-derivable cache; reconcile against manifests on resume"
}
```

### matrix.rows の要素 (build 開始時に生成)

```jsonc
{ "tfl": "int_orders", "layer": "int",
  "pipeline": { "extract":"done","analyze":"done","decompose":"done",
                "build":"pending","publish":"pending","compare":"pending" },
  "crosscut": { "schedule":"n/a","repoint":"n/a","backfill":"n/a" } }
```

## 必須 / nullable の別

| 群 | フィールド |
|---|---|
| **必須 (init で non-null)** | `meta` / `scope` / `migration_order` / `backfill_candidates` (空配列可) / `human_queue` / `pointers` |
| **nullable (後段で埋まる)** | `trigger_policy` / `old_schedule_notes` / `matrix.rows` (init は `[]`) / `session_batches` (single では `null`) |

## フィールド詳細

- **meta.goal_stage**: init_plan の `--goal <1-7>` を表示ラベルに変換して格納。判定ロジックは human_queue 構成に使う整数を別途参照しない (human_queue は init 時に確定済み)。
- **migration_order.order**: 複数フローは `flow-dependencies.json` の `topological_order` を採用 (producer 先行)。順序の**採否**は決定 (Stop 1 で追認)、値の出所はファクト (`source` にポインタ)。
- **backfill_candidates**: 各 in-scope フローの facts の `incremental` オブジェクト (`{run_type, control_fields}` のネスト形) から機械抽出。`incremental.run_type == "incremental"` の flow のみ候補化し、`control_field` は `incremental.control_fields[0]`。出力エントリ自体は flat (`{flow, control_field, applicable, mode, reason}`)。`mode` は init では常に `null` (compare 後にユーザーが seam/replace を決める)。
- **trigger_policy**: 散文文字列を基本とする (prep-schedule-designer の `trigger_policy` 引数と同じ散文契約)。構造化 dict `{tz, domains:[{name, weekday_constraint}]}` も許容し、render_migration_plan.py は両対応。
- **human_queue**: `--crosscut` と backfill 候補から init 時に骨を組む。各ステップの `action` 中身の詳細 (対象 WB・Linked Task メンバー) は各工程の runbook 生成時に `runbook_ref` で紐付ける。
- **status (全般)**: 再導出キャッシュ。init は全 pending。正本は manifest 群 / verify 出力。

## migration-plan.md テンプレート

`.md` は `render_migration_plan.py` が JSON から生成する (手編集しない)。single/multi で分岐する。

### multi・init 直後 (Stop 1 提示時点)

```markdown
# Migration Plan — <target_path>   (created: <created_marker>)

## Scope
in-scope:  <in_scope をカンマ区切り>    out-of-scope: <out_of_scope or (なし)>    goal: <goal_stage>

## Migration order  ← 根拠: <pointers.flow_dependencies>
1. <order[0]>  2. <order[1]>  ...   (producer 先行)

## Migration matrix
(decompose 後に分解後 .tfl 単位で描画。status はそこから追跡)

## Trigger policy
(schedule 工程で確定 / crosscut に schedule が無ければ N/A)

## Backfill
候補: <backfill_candidates を control field 付きで列挙 or (なし)>
mode(seam/replace) は compare 後に決定

## Human work queue
1. [<trigger_condition>]  <action>   → (<runbook 待ち> or runbook_ref)
...

## Pointers（ファクトの出所 — 転記しない）
<flow_dependencies> / <deploy_context> / manifests[<n 件>]
```

### multi・build 開始後の matrix (格子が生えた状態)

```markdown
## Migration matrix
                 │ extr anlz dcmp bild pub  cmp │ sched repnt bkfl
─────────────────┼─────────────────────────────┼──────────────────
<tfl 名>         │ ...
凡例: ○=適用/pending ―=対象外(stg) done/fail/part=進捗 wip=進行中 n/a=非該当
```

### single (薄い版)

order / session_batches を畳み (1 フローで自明)、`human_queue` を主役にする。scope は 1 行、matrix は decompose 後、trigger_policy は crosscut に応じて N/A。

## レンダリング規則

- nullable が `null` の章は `(＜工程＞で確定)` プレースホルダを表示 (空にしない — 未充填が可視化されることが忘れ防止の要)。
- `matrix.rows` が空なら matrix 章はプレースホルダ、非空なら ASCII 格子を描画。
- `flow_count == "single"` は Migration order / Session batches 章を省略。
- 数値・LUID・run-type といったファクトは md に**転記しない** (Pointers 経由で出所を指すだけ)。
