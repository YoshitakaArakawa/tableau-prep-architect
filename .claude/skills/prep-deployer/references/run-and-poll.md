---
purpose: publish 済み flow の実行とジョブステータス polling の手順
note: Run 開始 / status 取得 / finishCode 判定 / 並列実行の排他制約 / タイムアウト方針を規定。REST API エンドポイント仕様も含む。失敗分類は autonomous-recovery.md に委譲
---

# run-and-poll

publish 済みの Prep flow を REST API で実行し、ジョブステータスを polling で取得して成功/失敗を判定するワークフロー。

## 目次

- REST API エンドポイント / tableauserverclient での等価コード / `finishCode` の意味
- スクリプトの使い方 / Polling 設計 / 失敗時の `notes` フィールド
- 並列実行と排他 (同一 OAuth session の制約と run_layer.py)
- run 後の manifest 更新 / 全レイヤ完走後の resolve-luids / REST API バージョン

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

承認は session intake (prep-migrate の step 0) で済んでいる前提 ([autonomous-recovery.md §実行ポリシー](autonomous-recovery.md))。

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

ただし notes は人間向け文字列で **構造化されていない**。notes パターン → root cause → 修正アクションの分類表は [autonomous-recovery.md の Run 失敗分類](autonomous-recovery.md) に集約。本ファイル固有の注意は **client 側 timeout** のみ: flow が想定より長い場合は `--timeout` を増やすか flow を分割する (timeout はジョブ失敗ではなく観測打ち切り)。

## 並列実行と排他

### Tableau Cloud の認証モデル制約 (重要)

**Tableau Cloud は同一ユーザー identity で 1 active session のみ許可する**。新しい sign-in (OAuth ブラウザフロー or REST `/auth/signin`) が走った瞬間、以前発行された credential token は server-side で即座に revoke される。`tableauserverclient` 側に invalidation ロジックは無く (`Server` インスタンスは独立した `_auth_token` を保持するだけ)、これは Tableau Cloud 側の認証仕様。

検証: 同一プロセス内で `signed_in_server()` を 2 回続けると、1 回目の token を使った API call が 401 (`401002 Invalid authentication credentials`) を返す。同一スレッド外でも threaded で並列サインインしたワーカーは、後発のサインイン後最初の API call で同様に 401。

### 並列可否の場合分け

| 操作 | 並列可否 | 根拠 |
|---|---|---|
| 同じ flow を同時に複数 run | ❌ | server 側で同一 flow の同時 run リクエストが拒否される |
| 異なる flow を `--wait` で同一ユーザー並列起動 | ❌ | 後発 `run_flow.py` のサインインが server 側で先発 token を revoke。先発の polling は 401 で死ぬ (先発の job 自体は server-side で完走するが client は finishCode を観測できない) |
| 異なる flow を `--no-wait` で **時間的に重ならない sequential 起動** + 単一プロセスで後追い polling | ✅ | 各 `--no-wait` 呼び出しは数秒で sign-in → POST → sign-out が閉じる (※ OAuth はブラウザサインインのオーバーヘッドあり)。次の呼び出しとサインインが重ならないので token 競合なし。server-side では job が並列実行される |
| 同じ flow を順次 run (DAG 連鎖) | ✅ | `--wait` で前段の完了を待ってから次 |
| **別ユーザー identity で `--wait` 並列** | ✅ | identity が異なれば session は独立。OAuth の場合、別ユーザーアカウントで都度サインインし直すコストと引換 |

### 推奨パターン: server-side parallel + client-side serial signin

依存関係のない flow (例: 同一レイヤ内の独立 stg flow) を並列実行したい場合の standard パターン。**実装は [scripts/run_layer.py](../../../../scripts/run_layer.py) に集約済**。

```bash
# manifest の <layer> 内で publish=published かつ run!=success の全件を対象に、
# Step 1: 各 flow を --no-wait で sequential 起動 (sign_in は順番に発生、競合しない)
# Step 2: 単一 sign-in session で全 jobId を順次 polling
# Step 3: 完了した flow ごとに publish_manifest.py update-run を呼ぶ
python scripts/run_layer.py \
  --manifest work/<session>/reports/publish-manifest.json \
  --layer staging \
  --poll-interval 15 \
  --timeout 1800
```

`run_layer.py` の動作は本ファイル §並列実行と排他 の制約を運用に落としたもの。server-side では job が並列実行されるため wall-clock は `max(run_durations)` で済む (sequential 合計ではない)。Polling は 1 セッションで `server.jobs.get_by_id` を順次呼ぶだけなので token 競合は起きない。

リトライは行わない (失敗 1 件で exit 1)。recovery は呼び出し側 (prep-deployer の SKILL ループまたはユーザー) が [autonomous-recovery.md](autonomous-recovery.md) のマッピングで判定する。

手で同等のことをやりたい場合 (debug 等) は以下:

```bash
python run_flow.py --flow-name "stg_orders"    --no-wait    # → RESULT_JSON jobId_A
python run_flow.py --flow-name "stg_customers" --no-wait    # → RESULT_JSON jobId_B
python get_job_status.py --job-id <jobId_A>
python get_job_status.py --job-id <jobId_B>
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
