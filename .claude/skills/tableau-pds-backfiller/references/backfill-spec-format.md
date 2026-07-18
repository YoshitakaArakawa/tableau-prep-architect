---
purpose: tableau-pds-backfiller の入力 spec JSON と、backfill_pds.py が書き出す audit manifest JSON の全フィールド仕様
note: SKILL.md からこの reference にリンクする。spec の全フィールドと manifest の全フィールドを 1 箇所で網羅する
---

# backfill spec / manifest フォーマット

`backfill_pds.py --spec <path>` と `publish_preview.py --spec <path>` に渡す入力 spec、および `backfill_pds.py` が出力する `backfill-manifest.json` の仕様。

## 目次

- 1 エントリの単位
- spec 例
- `flows[]`
- `flows[].column_map[]`
- audit manifest (`backfill-manifest.json`)

## 1 エントリの単位

1 エントリ = (旧 PDS 1 個: 履歴の供給源) + (新 accumulator PDS 1 個: seed 先) + (control field) + (seam / replace モード) → (新 accumulator を Overwrite で 1 回 seed)。

- `tag` はセッション内で一意な短い識別子 (`f02` 等)。`--only <tag>` と manifest / snapshot のキーに使う
- spec は複数エントリを持てる (バッチ backfill)。ただし **エントリ間に順序依存は無い** (各 accumulator は独立)。1 つずつ `--only` で回すのが既定 (承認ゲートをエントリ単位で握るため)

## spec 例

```json
{
  "flows": [
    {
      "tag": "f02",
      "old_luid": "<old-archived-pds-luid>",
      "new_luid": "<new-accumulator-pds-luid>",
      "control": "Date",
      "mode": "seam"
    },
    {
      "tag": "f06",
      "old_luid": "<old-pds-luid>",
      "new_luid": "<new-accumulator-pds-luid>",
      "control": "Report Date",
      "mode": "replace",
      "column_map": [
        { "new": "Report Date", "old": "ReportDate" },
        { "new": "EPS", "old": "Eps", "cast": "double" }
      ]
    }
  ]
}
```

## `flows[]`

| フィールド | 必須 | 説明 |
|---|---|---|
| `tag` | ✅ | セッション内で一意な識別子。manifest / snapshot / `--only` のキー |
| `old_luid` | ✅ | 履歴の供給源 = 旧 output PDS の LUID。**backfill 確定まで削除しない** (再構築不能な唯一の履歴源) |
| `new_luid` | ✅ | seed 先 = 新 incremental accumulator PDS の LUID。**必ず `resolve_accumulator.py` が deployed flow から解決した LUID** を使う (計画書 / manifest の LUID はドリフトしうる) |
| `control` | ✅ | incremental control field 名 (**新 accumulator 側の名前**)。旧側で名前が違うなら `column_map` で対応付ける |
| `mode` | no | `seam` (default) または `replace`。判断基準は [preconditions-and-edge-cases.md](preconditions-and-edge-cases.md) の sentinel 節。**フロー単位のユーザー判断**で、自動選択しない |
| `column_map` | no | 旧→新で列名 / 型が違う場合の対応付け (下表)。省略時は全列 name 一致・型一致を前提 (`diff_pds_schema.py` で事前検証する) |

- `mode: seam` — 旧の `control < MIN(新の control)` の行のみ挿入。新 baseline 区間 `[seam, new_max]` は新を正として温存、二重化しない。MAX(control) 不変
- `mode: replace` — 新の既存内容を DELETE して旧を全ロード。sentinel/placeholder baseline (control が実データ範囲外の far-past 値だけ) の救済用。watermark は old_max になる

## `flows[].column_map[]`

旧 PDS の物理列を新 accumulator の列へ name-align で対応付ける。**列順には依存しない** (name ベース)。

| フィールド | 必須 | 説明 |
|---|---|---|
| `new` | ✅ | 新 accumulator 側の列名 (INSERT の対象列) |
| `old` | ✅ | 旧 PDS 側の対応する列名 (SELECT の元列) |
| `cast` | no | 旧列を挿入前に変換する Hyper 型 (`double` / `bigint` / `text` / `date` / `timestamp` 等)。型不一致を吸収する場合のみ |

- 未マッピングの新列は **同名の旧列** から挿入 (identity)。同名の旧列が無ければ `backfill_pds.py` は **escalate (RuntimeError)** する — rename では吸収できないスキーマ差なので、`column_map` を足すか設計に戻る
- `control` の列名が旧側で違う場合も `column_map` に含める (seam 比較の WHERE 句が旧側の対応列を使う)
- **control の型は旧・新で一致していること** (seam 比較が同型前提)。型が違う場合は `diff_pds_schema.py` が検出するので、backfill 前に解消する

## audit manifest (`backfill-manifest.json`)

`backfill_pds.py` が `--workdir` 直下に追記する監査ログ。dry-run / execute / restore の全実行を記録し、再現性・監査・ロールバックの起点になる。

| フィールド (`entries[]`) | 説明 |
|---|---|
| `tag` / `mode` / `control` | エントリ識別とモード |
| `old_luid` / `new_luid` | 対象 PDS |
| `old_count` / `old_min` / `old_max` | 旧 PDS の行数と control 範囲 |
| `new_count_before` / `new_min` / `new_max` | seed 前の新 accumulator の行数と control 範囲 (`new_min` = seam) |
| `new_distinct_control` | 新の control の distinct 数 (sentinel 判定の傍証) |
| `seam` / `to_insert` / `expected_new_total` | seam 値・挿入行数・期待総行数 |
| `local_new_total_after` / `local_max_control_after` / `local_verify` | ローカル抽出への挿入後の実測と検算結果 (`OK` / `MISMATCH`) |
| `sentinel_warning` | seam モードで `new_max <= old_min` かつ挿入 0 のとき true (replace 推奨のサイン) |
| `snapshot_tdsx` | ロールバック点 (`--restore <tag>` が使う) |
| `backfilled_tdsx` | seed 済み抽出の現物 (preview publish の入力) |
| `executed` / `published_luid` | 本番 publish したか + 結果 LUID |
| `server_verify` | execute 時のサーバー再検証 (`server_count` / `server_max_control` / `verdict`) |
| `timestamp` | 実行時刻 (ローカル時刻 ISO) |

`restores[]` には `--restore` の記録 (`tag` / `restored_from` / `published_luid` / `timestamp`) が入る。
