---
purpose: tableau-pulse-repointer が使う Tableau Pulse REST API の機構・制約・確定レシピの実務リファレンス
sources:
  - https://help.tableau.com/current/api/rest_api/en-us/REST/rest_api_ref_pulse.htm
  - https://help.tableau.com/current/api/rest_api/en-us/REST/TAG/index.html#tag/Pulse-Methods
  - https://github.com/tableau/pulse-api-utilities
fetched_at: 2026-07-18
source_last_known_update: 不明 (取得時点の最新版)
note: 公式ドキュメント + 公式ユーティリティ実装 + 実機実験で確定した挙動を集約する。エンドポイント網羅ではなく、repoint に必要な機構と落とし穴に絞る
---

# Pulse API Recipe

## 目次
- 前提と認証
- 使うエンドポイント
- 実測で確定した制約 (落とし穴)
- 確定レシピ: datasource 差し替え
- insight probe (機能検証)

## 前提と認証

- Pulse REST は **versionless path `/api/-/pulse/...`** (site LUID はパスに含まれず sign-in
  コンテキストから決まる)。**Tableau Cloud 専用** — Tableau Server では使えない
- 認証は通常の REST セッショントークンを `X-Tableau-Auth` ヘッダで渡す。repo 共通の
  OAuth (PKCE) access_token (`scripts/tableau_auth.py` の `signed_in_server()`) がそのまま通る。
  追加スコープ設定が要るのは Connected Apps JWT / Unified Access Tokens を使う場合のみ
- 権限: 定義の作成・更新・削除には参照 datasource への write + publish 権限。読み取り系は
  閲覧権限のある定義だけが返る

## 使うエンドポイント

| 操作 | メソッド + パス | 備考 |
|---|---|---|
| 定義一覧 | `GET /api/-/pulse/definitions?view=DEFINITION_VIEW_FULL&page_size=100` | `next_page_token` を追従して全ページ取る (下記の落とし穴)。FULL で配下 `metrics[]` 同梱 |
| 定義取得 | `GET /api/-/pulse/definitions/{id}` | レスポンスは `{definition, candidate_definitions}` |
| 定義作成 | `POST /api/-/pulse/definitions` | 201。body = `{name, description, specification, extension_options, representation_options, insights_options, comparisons}` |
| 定義更新 | `PATCH /api/-/pulse/definitions/{id}` | 部分更新 (name のみ / certification のみ等)。**datasource 変更は不可 (下記)** |
| 定義削除 | `DELETE /api/-/pulse/definitions/{id}` | 204。**配下 metrics + subscriptions が連鎖削除** |
| metric 一覧 | `GET /api/-/pulse/definitions/{id}/metrics` | |
| metric 再作成 | `POST /api/-/pulse/metrics:getOrCreate` | body = `{definition_id, specification}`。`is_metric_created` で新規/既存判別 → 冪等 |
| 購読一覧 | `GET /api/-/pulse/subscriptions?metric_id=&page_size=200` | metric_id 省略で site 全件 (follower 棚卸し) |
| 購読作成 | `POST /api/-/pulse/subscriptions` | body = `{metric_id, follower: {user_id \| group_id}}` → 201 |
| insight 生成 | `POST /api/-/pulse/insights/ban` | 201。機能検証に使う (下記) |

定義オブジェクトの datasource 参照は **`definition.specification.datasource.id`** (PDS の LUID)。
定義自体の ID は `definition.metadata.id`。

## 実測で確定した制約 (落とし穴)

1. **一覧の既定 page_size は 10 で silent truncation する**。`total_available` はレスポンスに
   あるが定義配列は 1 ページ分しか返らない。必ず `page_size` を明示し `next_page_token` を追従する
2. **PATCH で `specification.datasource.id` を変更すると 404 "Not Found"** (新→旧・旧→新の両方向、
   同一 payload で ds 不変なら 200)。in-place の datasource 差し替えは現行 Cloud では不可。
   公式 pulse-api-utilities もコピー作成方式を採っている
3. **create はほぼ何も検証しない**: viz_state 内の `sqlproxy.<id>` ラベルがデタラメでも、参照
   フィールド名が存在しなくても 201 で通る。不整合は insight 生成時 (実行時) に 400 で顕在化する
4. **query 時の正は `specification.datasource.id`**。viz_state 内の `sqlproxy.<id>` ラベルは
   不活性 (解決不能なラベルでも正しい datasource から値が返ることを実測確認)。コピー作成時に
   viz_state を書き換える必要はない
5. **DELETE の連鎖**: 定義を消すと配下 metrics と subscriptions (follower) も消える。follower
   移行が終わる前に旧定義を消してはならない。本 Skill が旧定義削除を人間判断に残すのはこのため
5b. **同一 (datasource + specification) の定義は 2 つ作れない** (POST が 409 Conflict、実測)。
   rehearsal コピーは新 PDS 上に対象 spec の定義として既に存在するため、production では新規
   create すると rehearsal コピーと衝突する。→ **rehearsal コピーを rename して昇格** (promote)
   し、それを production 定義として使う。新規 create は rehearsal を回さなかった場合のみ
6. metric を別定義に付け替える API は無い (`UpdateMetricRequest` に definition_id が無い)。
   移行は `metrics:getOrCreate` での再作成一択
7. WB repoint の Metadata API lineage (`downstreamWorkbooks`) には **Pulse 消費は写らない**。
   Pulse の棚卸しは本 recipe の definitions 走査でしかできない
8. **制約 2 と 5b は未文書の実行時挙動** (実測)。公式 OpenAPI spec は `UpdateDefinitionRequest` に
   `specification` (datasource 含む) を正規フィールドとして持ち、immutable 注記も 409 の定義も
   無い — スキーマと実挙動が食い違う領域。傍証: Tableau 公式 pulse-api-utilities の
   "Swap Datasources" も in-place PATCH ではなくコピー作成 + metric/follower 移送で実装されている
   (定義への PATCH は certification 除去にしか使っていない)

## 確定レシピ: datasource 差し替え

旧定義 D_old (旧 PDS 参照) を新 PDS へ差し替える:

1. **rehearsal**: D_old の FULL ビューから payload を組み、`specification.datasource.id` だけ
   新 PDS LUID に差し替えて `<元名> (repoint rehearsal)` で POST → insight probe で新 PDS 上の
   機能を確認 (元定義は無傷)
2. **production** (ユーザー承認後):
   a. D_old を `<元名> (pre-repoint)` に rename (PATCH)
   b. rehearsal コピー (`<元名> (repoint rehearsal)`、新 PDS) を `<元名>` に rename して **昇格** =
      D_new とする (5b: 同一 datasource+spec を再 POST すると 409。rehearsal を再利用する)。
      rehearsal を回していない場合のみ新規 POST
   c. D_old 配下の metric と購読を **実行時にライブで読み直し** (design スナップショットを
      信じない — follower は design 後にも増減する)、non-default metric を
      `metrics:getOrCreate` (definition_id=D_new) で再作成
   d. 旧 metric ごとのライブ follower を、対応する新 metric へ `POST /subscriptions` で再作成
      (default → 新 default、non-default → getOrCreate した対応 metric。既存購読はスキップ = 冪等)
   e. D_new で insight probe (失敗時はここで中断 — D_old は rename だけで生きている)
   f. 昇格でなく新規 create した場合のみ残った rehearsal コピーを削除 (follower ゼロ確認 +
      昇格済み id を消さないガード付き)
3. D_old (`(pre-repoint)`) の削除は人間判断 (連鎖削除の明記付きで runbook に手順を残す)

default metric は定義作成時に自動生成されるため再作成不要 (c は non-default のみ)。

## insight probe (機能検証)

定義が新 PDS 上で機能するか (フィールド整合を含む) は insight 生成でしか確認できない:

```
POST /api/-/pulse/insights/ban
{
  "bundle_request": {
    "version": 1,
    "options": {"output_format": "OUTPUT_FORMAT_TEXT", "time_zone": "Asia/Tokyo"},
    "input": {
      "metadata": {"name": <定義名>, "metric_id": <metric id>, "definition_id": <定義 id>},
      "metric": {
        "definition": {specification の datasource / viz_state_specification / is_running_total},
        "metric_specification": <metric の specification>,
        "extension_options": ..., "representation_options": ..., "insights_options": ...
      }
    }
  }
}
```

201 で `bundle_response.result.insight_groups[].insights[].result.markup` に BAN の文字列
(例: "X was $365.0 (2026 year to date), up 50.1% ...") が返る。**400 はフィールド不整合**
(新 PDS に定義参照フィールドが無い) のシグナル。rehearsal では元定義とコピーの markup を比較し、
値まで一致すれば差し替えは安全と判定できる (freshness 差による軽微な differs は想定内)。

## 未確認事項

- `POST /subscriptions` の実書込み挙動 (API 形は公式ドキュメント + 公式ユーティリティ実装で
  裏取り済みだが、重複作成時の応答・group follower の展開は実機未確認)
- 定義 rename が Pulse UI 表示・digest メールへ与える影響
