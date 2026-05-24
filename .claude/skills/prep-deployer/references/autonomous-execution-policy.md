---
purpose: prep-deployer の「実行系操作は AI Agent が承認なしで自律実行し、失敗は自律ループで回復する」ポリシー定義
fetched_at: 2026-05-17
note: publish / run の副作用、承認を取らない判断根拠、--yes フラグの扱い、自律リトライと escalation の境界を規定
---

# autonomous-execution-policy

`prep-deployer` の **「publish / run は AI Agent が承認なしで自律実行し、失敗時は自律診断ループで回復、回復不能な種類だけ人間に escalation する」** ポリシーを規定する。

## なぜ承認を撤廃するか

承認プロンプトの本来の目的は「production への副作用を取り消しにくいので人間に最終判断を委ねる」だった。だがこのリポジトリのワークフローでは:

- **Session intake (step 0)** で **ゴール段階 (Q2)** と **target path (Q4)** をユーザーから明示で取っている。「Cloud に publish & run まで」を選んだ時点で、その target 配下への書き込みは合意済み
- target は `99_Sandbox/...` のような **隔離されたサブツリー** であり、最下層 3 レイヤ (`stg/intermediate/marts`) も規約で固定。誤って組織全体に影響を与える経路がない
- レイヤごとに承認を取り直しても、人間は中間 finishCode を見てジャッジできない (元 .tfl の挙動を覚えていないため、ノイズになる)
- 失敗時に「どこが壊れているか」は finishCode / notes / errorCode から AI が機械的に判定できるケースが大半

「session 開始時の 1 回の合意で、レイヤごとの細かい承認は省く」方が、ユーザーにとってもノイズが少なく、AI にとっても自律ループを組みやすい。

## 副作用は消えていない

承認を取らないことは **副作用を軽視すること** ではない。引き続き以下は事実:

### publish の副作用

- Tableau Cloud 上の既存 flow / Published DS を上書きする可能性
- target プロジェクト配下に新規 flow を作成

### run の副作用

- **入力ソースからのデータ読み込み** (仮想接続経由でも本番 DB に負荷)
- **出力先への書き込み** — Published DS の上書き、出力 DB テーブルの再生成
- 既に publish 済みのスケジュールジョブと衝突しうる
- Extract 容量を消費

これらが本当に「やってよい」かどうかは **session intake で確定した target path** が安全領域である前提で AI が判断する。target が本番領域ならそもそも session intake の時点でユーザーが拒否すべき。

## 3 つの不変条件

1. **AI Agent は publish / run を承認プロンプトなしで実行する**
   - `--yes` は AI Agent がデフォルトで付与する (旧ポリシーから反転)
   - スクリプト側の対話プロンプトは撤廃済み (`publish_flow.py` / `run_flow.py` / `create_project.py`)

2. **失敗は自律診断ループで回復を試みる**
   - エラー種別 → 修正アクションのマッピングは [autonomous-recovery.md](autonomous-recovery.md) を参照
   - リトライ上限: publish 最大 3 回 / run 最大 2 回。同じ errorCode で 2 回連続失敗したらループ停止
   - 回復不能な種別 (credential 失効 / ライセンス不足 / Cloud 側 5xx / メンテ中) は即 escalation

3. **escalation は素直に報告する**
   - 失敗を握り潰さない。finishCode / notes / errorCode をそのまま会話に返す
   - 「N 回試行して同じエラー」を検知したら自動でこれ以上試さず人間に判断を仰ぐ
   - 自動ロールバックはしない (Tableau Cloud のバージョン履歴から **手動で戻す方が監査ログがクリーン**)

## 承認をいまも取る場面

| 場面 | 承認方法 |
|---|---|
| Session intake (step 0) で goal / target path を確定 | 会話で明示合意 (これが publish / run 全体の承認になる) |
| top-level プロジェクト作成 (preflight で `existing_prefix` が null) | WARNING を stderr に出すが処理は止めない (governance はユーザーが事後監査) |
| CI / cron での無人実行 | session intake 相当の合意を別途持つ (PR レビュー / ChatOps 等) |

## 環境固有の「実行不可」ケース

以下は引き続き「AI では回復できない」ので即 escalation:

| 状況 | 検出方法 | 対処 |
|---|---|---|
| サインインしたユーザーのライセンスが Creator でない | publish が 403 | escalation (人間 → 管理者) |
| access token 失効 (>1〜2h) | 401 | escalation (人間 → プロセス再起動で再 OAuth サインイン) |
| 仮想接続の DB 認証情報失効 | run finishCode=1 + notes に "authentication" | escalation (人間 → 接続情報更新) |
| サイト容量上限 | publish or run が 5xx | escalation |
| メンテナンスウィンドウ中 | sign-in 失敗 | escalation (待つしかない) |

これらの判定は [autonomous-recovery.md](autonomous-recovery.md) の「escalation 対象」表に集約。AI は notes 文字列にパターンマッチして判定する。

## CI での運用

本リポの `signed_in_server()` は OAuth ブラウザサインイン前提なので **CI では使えない**。CI/CD で非対話 publish が必要になった場合は、本リポのスコープ外として **別途 PAT ベースの簡易 REST スクリプト** を切り出す前提とする (例: `secrets.TABLEAU_PAT_NAME` / `TABLEAU_PAT_VALUE` を `X-Tableau-Auth` で直接 sign-in する小さな publish スクリプト)。承認は環境 (production) に Required reviewer を設定する形で session intake と等価の合意ゲートを置く。

## 旧ポリシーからの変更点 (2026-05-17)

旧 `user-approval-policy.md` との差分:

| 項目 | 旧 | 新 |
|---|---|---|
| publish / run 前の承認 | スクリプトが `[y/N]` プロンプト、Skill も主会話で確認 | **撤廃**。session intake の合意のみ |
| `--yes` フラグ | AI Agent は付与禁止 | **撤廃** (スクリプトが常に非対話) |
| 失敗時の自動再試行 | 禁止 | **許可** (上限・loop 検知付き) |
| 自動ロールバック | 禁止 | 引き続き禁止 (監査ログ保全のため) |
| 「実行不可」の扱い | 握り潰さず報告 | 引き続き握り潰さず escalation |
