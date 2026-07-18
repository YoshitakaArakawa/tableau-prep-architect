---
purpose: prep-deployer の自律実行ポリシーとリトライループ仕様。承認なし実行の根拠、publish / run 失敗時の symptom→root cause→修正アクション マッピング、escalation 境界を規定
note: 実行ポリシー (なぜ承認を取らないか) と回復ループ (失敗をどう分類しどこまで自動修正するか) を 1 ファイルに集約。リトライ上限、loop 検知、escalation 条件、回復不能エラーの一覧を含む
---

# autonomous-recovery

`prep-deployer` の **実行ポリシーと自律リトライループ** の仕様。publish / run / preflight を承認なしで実行する根拠と、失敗したとき AI Agent がどう原因を判定し、どこまで自動で修正・再試行し、どこから人間に escalation するかを規定する。

## 目次

- 実行ポリシー: 承認なしの自律実行
- ループの基本構造 / リトライ上限
- Publish 失敗の分類と修正アクション
- Run 失敗 (finishCode=1) の分類と修正アクション
- Preflight 失敗の扱い
- escalation 対象 (回復不能)
- ロールバック方針
- レイヤ間順序の扱い

## 実行ポリシー: 承認なしの自律実行

publish / run / preflight は承認プロンプトを出さずに実行する (`--yes` は AI Agent がデフォルト付与、スクリプト側の対話プロンプトは撤廃済み)。根拠:

- **Session intake (step 0)** で goal (Q2a) と target path (Q4) をユーザーから明示で取っている。「Cloud に publish & run まで」を選んだ時点で target 配下への書き込みは合意済み
- target は `99_Sandbox/...` のような **隔離されたサブツリー** で、誤って組織全体に影響を与える経路がない
- レイヤごとに承認を取り直しても、人間は中間 finishCode をジャッジできない (ノイズになる)

承認を省くことは副作用の軽視ではない: publish は既存 flow / Published DS の上書き・新規作成、run は入力ソースへの負荷 (仮想接続経由でも本番 DB)・出力 PDS / テーブルの上書き・Extract 容量消費を伴う。target が安全領域であることは session intake の時点でユーザーが担保する。

例外の扱い: top-level プロジェクト作成 (preflight で `existing_prefix` が null) は WARNING を stderr に出すが処理は止めない (governance は事後監査)。CI / cron の無人実行は本リポのスコープ外 — `signed_in_server()` は OAuth ブラウザサインイン前提で CI では使えないため、必要なら PAT ベースの簡易スクリプトを別途切り出し、Required reviewer 等で session intake 相当の合意ゲートを置く ([authentication.md](authentication.md))。

## ループの基本構造

```
publish or run 実行
    ↓
成功 (finishCode=0 / HTTP 200) → 次のステップへ
失敗 → エラー分類
    ↓
回復可能なエラー → 修正アクション → 再実行 (リトライ上限まで)
回復不能なエラー → 即 escalation (人間に報告して停止)
リトライ上限到達 / loop 検知 → escalation
```

## リトライ上限

| 操作 | 最大試行回数 | loop 検知 |
|---|---|---|
| publish | 3 回 | 同じ errorCode で 2 回連続失敗 → 停止 |
| run | 2 回 | finishCode=1 が 2 回連続 → 停止 |
| preflight (create_project) | 1 回 | 再試行しない (idempotent なので部分作成は次回 preflight で続きから) |

「同じ errorCode で 2 回連続」が **loop 検知** の primitive。AI が「修正したつもり」でも同じエラーが返るなら修正できていない証拠なので、それ以上消費しない。

## Publish 失敗の分類と修正アクション

| Tableau errorCode | symptom | root cause | 修正アクション | 担当 |
|---|---|---|---|---|
| `280003` | HTTP 400 "Problem reading the provided Flow file" | (a) maestroMetadata 欠落 / (b) Input ノードに connection 登録なし (孤立 connectionId) / (c) 複数の重複 Tableau Server connection entry (KB 005232681) / (d) LoadSqlProxy / dataConnection の `dbname` 欠落 / (e) LoadSqlProxy 必須デフォルトフィールド欠落 | .tfl を **再 build**。sub-cause 別: (a) `aux_entries=` 渡し忘れを確認 / (b) `flow_io.add_pds_input` で connection 一括登録 / (c) `add_pds_input` は dedup する — 自前生成を疑う / (d) `add_pds_input` は dbname=None でも placeholder を自動挿入する / (e) `make_load_sql_proxy_node` のデフォルトに含まれる — 自前構築なら要追加 | prep-builder |
| `409` | name conflict | 同名 flow が CreateNew で既存 | `--mode Overwrite` で再 publish | prep-deployer |
| `429` | rate limit | API リクエスト過多 | exponential backoff (1s → 2s → 4s) で同じ操作を再試行 | prep-deployer |
| `5xx` | server error / capacity | Cloud 側障害 or サイト容量上限 | **escalation** (AI では回復不可) | — |
| `401` | unauthorized | access token 失効 (>1〜2h) / セッション revoke | **escalation** (人間 → 同プロセスを再起動して再サインイン) | — |
| `403` | forbidden | サインインユーザーの権限不足 / ライセンス不足 | **escalation** | — |
| `404` (project) | parent missing | preflight 未実施 or project 削除 | **prep-extractor Phase B 再実行 → preflight → 再 publish** | prep-extractor / prep-deployer |
| `4xx` "Input data source not found" | 上流 PDS 不在 | 上流レイヤを先に publish & run していない | レイヤ順序を確認、上流から完走させる (publish-recipe.md の順次節) | prep-deployer |

実値 (上記以外のコード) を観測したら本表に追記して育てる。

## Run 失敗 (finishCode=1) の分類と修正アクション

`notes` フィールドの文字列マッチで分類する (notes は人間向けで構造化されていないが、典型パターンは存在):

| notes パターン | root cause | 修正アクション | 担当 |
|---|---|---|---|
| `Input data source not found` / `Input ... not exist` | 上流 PDS が Cloud に未 publish (= 上流レイヤ未完走) | 上流レイヤを完走させてから再 run | prep-deployer |
| `dbname` mismatch 系 | LoadSqlProxy の dbname が上流の実 Hyper 名と不一致 | `discover_pds_dbname.py` で実 dbname 取得 → `flow_io.patch_pds_dbname` で下流 .tfl 書き換え → 再 publish → 再 run | prep-builder / prep-deployer |
| `Unknown field name` / `Can't find field` / join clause の field 欠落 / incremental 制御列欠落 | 上流 PDS の実列名と flow の参照名が不一致 (上流 rename の喪失・スキーマ drift)。接続障害でも metadata 反映ラグでもない | 上流 PDS の published .tds と flow.json を DL して列名を突合 → 不一致の側 (上流 PDS or .tfl) を修正 | prep-builder / caller |
| `authentication error` / `authentication failed` | 仮想接続の DB 認証情報失効 | **escalation** | — |
| `insufficient permission` / `permission denied` | サービスアカウントの出力先プロジェクト書き込み権限不足 | **escalation** | — |
| `extract size exceeded` | サイト容量上限 | **escalation** | — |
| notes 空 + finishCode=1 | サーバー内部エラー | 1 度だけ自動再 run。再度同じなら **escalation** | prep-deployer |

`finishCode=2` (Cancelled) の扱い:
- 他者 / 管理者が手動キャンセル → 1 度だけ自動再 run
- 自動 timeout → escalation (timeout 値の再検討を含む)

## Preflight 失敗の扱い

preflight (`create_project.py` のループ) は idempotent なので、原則自動再試行しない:

| 状況 | 対処 |
|---|---|
| `create_project.py` が `403` (権限不足) | escalation |
| `create_project.py` が `404` (親プロジェクト消失) | prep-extractor Phase B 再実行で `deploy-context.md` を作り直し → 再 preflight |
| ループ途中で失敗 | 作成済みセグメントはそのまま残し escalation。原因解消後に同じコマンドで再 preflight (skip で続きから) |

## escalation 対象 (回復不能)

AI Agent が自力で回復しない種類:

1. **認証 / 権限系**: `401` / `403` / `notes` に "authentication" or "permission"
   - 理由: 人間がプロセスを再起動して再 OAuth サインイン / 権限付与しないと進めない
2. **容量 / ライセンス系**: `5xx` capacity / `notes` に "extract size exceeded" / "license"
   - 理由: 管理者 escalation が必要
3. **Cloud 側障害**: `5xx` / sign-in 失敗 / メンテナンスウィンドウ
   - 理由: 待つ以外の対処がない
4. **loop 検知発火**: 同じ errorCode で 2 回連続失敗 / リトライ上限到達
   - 理由: AI の修正アクションが効いていない証拠なので人間ジャッジが必要

escalation の出し方:
- stderr に `[ESCALATE] reason: <一行説明>` を出力
- 主会話に「N 回試行したが同じエラーで停止。原因: X、推奨対処: Y」と報告
- 失敗の生 finishCode / notes / errorCode をそのまま添付 (握り潰さない)

## ロールバック方針

publish 失敗 / run 失敗時も **AI は自動ロールバックしない**。理由:

- Tableau Cloud のバージョン履歴は publish 単位で残っている。手で戻す方が監査ログとしてクリーン
- 「直前バージョン」が常に正しいとは限らない (architect が再分解した結果として publish しているので、直前 = 旧構造)
- ロールバックを自動化すると、AI の修正アクションと混ざってどの状態が「真」か追跡困難になる

失敗時は **元 flow を Cloud に残したまま** エラー報告 → 人間が必要なら Cloud UI から revert。

## レイヤ間順序の扱い

[migration-workflow](../../../../references/migration-workflow.md) が定める `stg → intermediate → marts` 順序は **承認ゲートではなく依存関係**。ループも以下のように回す:

```
for layer in [stg, intermediate, marts]:
    for flow in layer:
        publish(flow)  # 失敗時は本ファイルの publish 分類でリトライ or escalation
    for flow in layer:
        run(flow)      # 失敗時は本ファイルの run 分類でリトライ or escalation
    # 全 flow の finishCode=0 を確認してから次レイヤへ進む
    # 1 flow でも escalation したら次レイヤに進まずユーザーに報告
```

レイヤ間に人間プロンプトは挟まないが、**上流レイヤで escalation が出たら下流に進まない** のは堅持。理由: 下流 flow の Input が上流 PDS を参照しているので、上流が壊れたまま下流を回すと finishCode=1 の連鎖になるだけ。
