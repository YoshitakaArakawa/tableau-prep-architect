---
purpose: prep-deployer の自律リトライループ仕様。publish / run 失敗時の symptom→root cause→修正アクション マッピングと escalation 境界を規定
fetched_at: 2026-05-17
note: AI Agent が承認なしで自律実行する前提で、失敗時の診断・修正・再実行ループを規定する。リトライ上限、loop 検知、escalation 条件、回復不能エラーの一覧を含む
---

# autonomous-recovery

`prep-deployer` の **自律リトライループ** の具体仕様。publish / run / preflight が失敗したとき、AI Agent がどう原因を判定し、どこまで自動で修正・再試行し、どこから人間に escalation するかを規定する。

承認方針は [autonomous-execution-policy.md](autonomous-execution-policy.md) を参照。本ファイルは「承認は session intake で済んでいる」前提で、**失敗→回復ループ** の挙動だけを規定する。

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
| `280003` | HTTP 400 "Problem reading the provided Flow file" | (a) maestroMetadata 欠落 / (b) Input ノードに connection 登録なし (孤立 connectionId) / (c) 複数の重複 Tableau Server connection entry (KB 005232681) / (d) LoadSqlProxy / dataConnection の `dbname` 欠落 / (e) LoadSqlProxy 必須デフォルトフィールド欠落 | .tfl を **再 build** (`flow_io.add_pds_input` で connection 一括登録、`aux_entries=` で maestroMetadata 同梱) | prep-builder |
| `409` | name conflict | 同名 flow が CreateNew で既存 | `--mode Overwrite` で再 publish | prep-deployer |
| `429` | rate limit | API リクエスト過多 | exponential backoff (1s → 2s → 4s) で同じ操作を再試行 | prep-deployer |
| `5xx` | server error / capacity | Cloud 側障害 or サイト容量上限 | **escalation** (AI では回復不可) | — |
| `401` | unauthorized | PAT 失効 | **escalation** (人間 → PAT 再発行) | — |
| `403` | forbidden | サービスアカウント権限不足 / ライセンス不足 | **escalation** | — |
| `404` (project) | parent missing | preflight 未実施 or project 削除 | **prep-extractor Phase B 再実行 → preflight → 再 publish** | prep-extractor / prep-deployer |
| `4xx` "Input data source not found" | 上流 PDS 不在 | 上流レイヤを先に publish & run していない | レイヤ順序を確認、上流から完走させる (publish-recipe.md の順次節) | prep-deployer |

実値 (上記以外のコード) を観測したら本表に追記して育てる。

## Run 失敗 (finishCode=1) の分類と修正アクション

`notes` フィールドの文字列マッチで分類する (notes は人間向けで構造化されていないが、典型パターンは存在):

| notes パターン | root cause | 修正アクション | 担当 |
|---|---|---|---|
| `Input data source not found` / `Input ... not exist` | 上流 PDS が Cloud に未 publish (= 上流レイヤ未完走) | 上流レイヤを完走させてから再 run | prep-deployer |
| `dbname` mismatch 系 | LoadSqlProxy の dbname が上流の実 Hyper 名と不一致 | `discover_pds_dbname.py` で実 dbname 取得 → `flow_io.patch_pds_dbname` で下流 .tfl 書き換え → 再 publish → 再 run | prep-builder / prep-deployer |
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
   - 理由: 人間が PAT 再発行 / 権限付与しないと進めない
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

CLAUDE.md の Workflow が定める `stg → intermediate → marts` 順序は **承認ゲートではなく依存関係**。ループも以下のように回す:

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

## 関連ドキュメント

- 承認ポリシー全体像: [autonomous-execution-policy.md](autonomous-execution-policy.md)
- publish の具体手順: [publish-recipe.md](publish-recipe.md)
- run / polling の具体手順: [run-and-poll.md](run-and-poll.md)
- preflight アルゴリズム: [preflight-recipe.md](preflight-recipe.md)
- 認証情報運用: [authentication.md](authentication.md)
