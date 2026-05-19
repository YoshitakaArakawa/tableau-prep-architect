---
purpose: publish 済み flow の実行とジョブステータス polling の手順
fetched_at: 2026-05-17
note: Run 開始 / status 取得 / finishCode 判定 / リトライ / タイムアウト方針を規定。REST API エンドポイント仕様も含む
---

# run-and-poll

publish 済みの Prep flow を REST API で実行し、ジョブステータスを polling で取得して成功/失敗を判定するワークフロー。

## REST API エンドポイント

### Run 開始

```http
POST /api/3.x/sites/{site-id}/flows/{flow-id}/run
Authorization: X-Tableau-Auth: <token>
```

レスポンス（抜粋）:

```xml
<tsResponse>
  <job id="<job-luid>" mode="Asynchronous" type="RunFlow" createdAt="..." />
</tsResponse>
```

→ `job.id` を控える。

### ステータス取得

```http
GET /api/3.x/sites/{site-id}/jobs/{job-id}
Authorization: X-Tableau-Auth: <token>
```

レスポンス（抜粋）:

```xml
<job id="..." type="RunFlow"
     createdAt="..." startedAt="..." completedAt="..."
     finishCode="0">
  <runFlowJobType>
    <flowRun id="..." flowId="..." />
  </runFlowJobType>
  <notes>...</notes>
</job>
```

## `tableauserverclient` での等価コード

```python
# Run 開始
job = server.flows.refresh(flow)
print(f"Job id: {job.id}")

# ステータス取得
job = server.jobs.get_by_id(job.id)
print(f"finish_code: {job.finish_code}, completed_at: {job.completed_at}")
```

## `finishCode` の意味

| code | 状態 | exit code |
|---|---|---|
| 0 | Success | 0 |
| 1 | Failed | 1 |
| 2 | Cancelled | 1 |
| (null) または `completed_at == None` | InProgress / Pending | — |

`get_job_status.py` はこの finishCode を **プロセスの exit code に転写** する（CI から判定しやすくするため）。

## スクリプトの使い方

### 単発実行 (デフォルト = 完了まで block)

```bash
python run_flow.py --flow-name "stg_orders" --project-name "Sales Analytics/stg"
# → Job id: 12345-...
# → 30 秒おきにステータス表示 → Finished: Success
# → RESULT_JSON: {"jobId":"...","finishCode":0,"notes":null,"durationSec":120,...}
```

スクリプトは常に非対話。`--wait` (デフォルト True) で完了まで block し、終了コードで成功/失敗を表現する。終了時に `RESULT_JSON: {...}` を 1 行 emit するので、AI Agent はこの行を parse して [autonomous-recovery.md](autonomous-recovery.md) の分類ループに渡す。

### fire-and-forget

```bash
python run_flow.py --flow-id <luid> --no-wait
# → Job id だけ取って即終了
# → RESULT_JSON: {"jobId":"...","status":"started",...}
python get_job_status.py --job-id <jobId>   # 後で個別に確認
```

承認は session intake (CLAUDE.md step 0) で済んでいる前提 ([autonomous-execution-policy.md](autonomous-execution-policy.md))。CI でも同じスクリプトをそのまま使う。

## Polling 設計

| パラメータ | 既定値 | 推奨 |
|---|---|---|
| `--poll-interval` | 30 秒 | 軽い flow なら 10s、重い flow なら 60s |
| `--timeout` | 3600 秒 (1 時間) | flow の典型実行時間 × 2-3 倍 |

`POST /flows/{id}/run` は非同期で即座に jobId を返し、実際の実行はサーバー側で進む。polling は **REST API の `GET /jobs/{id}` を叩くだけ** で、サーバー側の実行に影響を与えない（読み取り専用）。

## 失敗時の `notes` フィールド

`finishCode=1`（Failed）のとき、`notes` フィールドに失敗の理由が入ることが多い：

```
notes: Connection to source 'orders_db' failed: authentication error
notes: Output to Published Data Source 'fct_sales' failed: insufficient permission
notes: Extract refresh failed: max extract size exceeded
```

ただし notes は人間向け文字列で **構造化されていない**。CI で機械的にエラーパターンを判定したい場合は、`get_job_status.py` の出力をログに残し、後段でテキスト解析する。

## 典型的なエラーパターン

| ケース | 原因 | 対処 |
|---|---|---|
| `finishCode=1`, notes に "authentication" | 接続情報の embed が失効 / 仮想接続経由なのに権限不足 | サービスアカウントの権限 / 仮想接続の DB 認証情報を確認 |
| `finishCode=1`, notes に "permission" | サービスアカウントが出力先プロジェクトに書き込めない | プロジェクト権限を Editor に |
| `finishCode=1`, notes が空 | サーバー側内部エラー | Tableau Cloud のステータスページ確認、サポート問い合わせ |
| `finishCode=2`（Cancelled） | 他のユーザー / 管理者が手動でキャンセル | ログで誰がキャンセルしたか確認 |
| **timeout** | flow が想定より長い | `--timeout` を増やすか、flow を分割（dbt 風に細分化） |

## 並列実行と排他

### Tableau Cloud の認証モデル制約 (重要)

**Tableau Cloud は同一 PAT で 1 active session のみ許可する**。新しい sign_in が走った瞬間、以前発行された credential token は server-side で即座に revoke される。`tableauserverclient` 側に invalidation ロジックは無く (`Server` インスタンスは独立した `_auth_token` を保持するだけ)、これは Tableau Cloud 側の認証仕様。

検証: 同一プロセス内で `server.auth.sign_in(auth)` を 2 回続けると、1 回目の token を使った API call が 401 (`401002 Invalid authentication credentials`) を返す。同一スレッド外でも threaded で並列 sign_in したワーカーは、後発の sign_in 後最初の API call で同様に 401。

### 並列可否の場合分け

| 操作 | 並列可否 | 根拠 |
|---|---|---|
| 同じ flow を同時に複数 run | ❌ | server 側で同一 flow の同時 run リクエストが拒否される |
| 異なる flow を `--wait` で同一 PAT 並列起動 | ❌ | 後発 `run_flow.py` の sign_in が server 側で先発 token を revoke。先発の polling は 401 で死ぬ (先発の job 自体は server-side で完走するが client は finishCode を観測できない) |
| 異なる flow を `--no-wait` で **時間的に重ならない sequential 起動** + 単一プロセスで後追い polling | ✅ | 各 `--no-wait` 呼び出しは数百ミリ秒で sign_in → POST → sign_out が閉じる。次の呼び出しと sign_in が重ならないので token 競合なし。server-side では job が並列実行される |
| 同じ flow を順次 run (DAG 連鎖) | ✅ | `--wait` で前段の完了を待ってから次 |
| **別 PAT を 2 つ用意して `--wait` 並列** | ✅ | PAT が異なれば session は独立。組織で複数 PAT を発行するコスト (失効管理 × N) と引換 |

### 推奨パターン: server-side parallel + client-side serial signin

依存関係のない flow (例: 同一レイヤ内の独立 stg flow) を並列実行したい場合の standard パターン:

```bash
# Step 1: 各 flow を --no-wait で sequential 起動 (sign_in は順番に発生、競合しない)
python run_flow.py --flow-name "stg_orders"    --no-wait    # → RESULT_JSON jobId_A
python run_flow.py --flow-name "stg_customers" --no-wait    # → RESULT_JSON jobId_B
python run_flow.py --flow-name "stg_products"  --no-wait    # → RESULT_JSON jobId_C

# この時点で server-side では job_A / job_B / job_C が並列実行されている
# wall-clock は max(run_A, run_B, run_C) で済む (sequential 合計ではない)

# Step 2: 単一プロセスで全 jobId を順次 polling
python get_job_status.py --job-id <jobId_A>    # 完了まで block
python get_job_status.py --job-id <jobId_B>
python get_job_status.py --job-id <jobId_C>
```

`get_job_status.py` を順次走らせている間、各呼び出しは独立した sign_in / sign_out で完結するため token 競合は起きない。

DAG 連鎖 (前段の完了が次段の開始条件) の場合は従来通り `--wait` を `&&` で連結:

```bash
python run_flow.py --flow-name "stg_orders" && \
python run_flow.py --flow-name "int_orders_enriched" && \
python run_flow.py --flow-name "fct_sales"
```

## run 後の manifest 更新

各 run の `finishCode` を受け取ったら、[scripts/publish_manifest.py update-run](../../../../scripts/publish_manifest.py) で session manifest を更新する:

```bash
python scripts/publish_manifest.py update-run \
  --manifest <session>/reports/publish-manifest.json \
  --flow-name <decomposed_flow_name> \
  --finish-code <0|1|2>
```

finishCode から `run.status` (`success` / `failed`) はスクリプトが自動決定する。

## 全レイヤ完走後の resolve-luids

最後の marts レイヤまで run が完了したら、[scripts/publish_manifest.py resolve-luids](../../../../scripts/publish_manifest.py) を 1 回だけ呼んで manifest に残った null LUID を埋める:

```bash
python scripts/publish_manifest.py resolve-luids \
  --manifest <session>/reports/publish-manifest.json
```

このコマンドが解決するもの:

- `original.flow_luid` (init 時に null だった場合、flow_name から逆引き)
- `original.outputs[].luid` (Metadata API の `downstreamDatasources` から)
- `decomposed_flows[].publish.flow_luid` (update-publish で既に入っていれば skip)
- `decomposed_flows[].outputs[].luid` (Metadata API)

LUID が揃った manifest は [prep-output-comparator](../../prep-output-comparator/SKILL.md) がそのまま消費する。manifest 形式は [../../../../references/publish-manifest-format.md](../../../../references/publish-manifest-format.md)。

## REST API バージョン

`POST /flows/{id}/run` は REST API 3.3+ で利用可能。`use_server_version=True` を TSC で指定すれば自動追従する。
