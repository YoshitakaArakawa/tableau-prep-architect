---
source: https://help.tableau.com/current/api/rest_api/en-us/REST/rest_api.htm
fetched_at: 2026-05-17
source_last_known_update: 不明（Tableau Cloud 2024.x – 2026.x を想定）
note: prep-deployer が使う主要エンドポイント（Auth / Projects / Flows / Jobs）の生 HTTP + TSC 両方の早見表。リクエスト・レスポンスの XML/JSON 例を含む
---

# rest-api-cheatsheet

`prep-deployer` が使う Tableau REST API の主要エンドポイント早見表。`tableauserverclient` (TSC) と生 HTTP の両方で記載。バージョンは Tableau Cloud 2024.x – 2026.x を想定。

## 認証

### Sign In（PAT）

```http
POST /api/3.x/auth/signin
Content-Type: application/json

{
  "credentials": {
    "personalAccessTokenName":   "my-pat",
    "personalAccessTokenSecret": "<secret>",
    "site": { "contentUrl": "mysite" }
  }
}
```

レスポンスから `token` と `site.id` を取得 → 以降のリクエストの `X-Tableau-Auth` ヘッダーに付与。

### TSC 等価

```python
auth = TSC.PersonalAccessTokenAuth("my-pat", "<secret>", site_id="mysite")
server = TSC.Server("https://10ax.online.tableau.com", use_server_version=True)
with server.auth.sign_in(auth):
    ...
```

## Projects

| 操作 | HTTP | TSC |
|---|---|---|
| 一覧 | `GET /sites/{site-id}/projects` | `server.projects.get()` |
| 取得 | `GET /sites/{site-id}/projects/{project-id}` | `server.projects.get_by_id(id)` |
| 作成 | `POST /sites/{site-id}/projects` | `server.projects.create(item)` |
| 更新 | `PUT /sites/{site-id}/projects/{project-id}` | `server.projects.update(item)` |
| 削除 | `DELETE /sites/{site-id}/projects/{project-id}` | `server.projects.delete(id)` |

ネストプロジェクトは `parentProjectId` で親を指定。

## Flows

| 操作 | HTTP | TSC |
|---|---|---|
| 一覧 | `GET /sites/{site-id}/flows` | `server.flows.get()` |
| 取得 | `GET /sites/{site-id}/flows/{flow-id}` | `server.flows.get_by_id(id)` |
| **publish** | `POST /sites/{site-id}/flows?overwrite=...` (multipart) | `server.flows.publish(item, file, mode)` |
| **download** | `GET /sites/{site-id}/flows/{flow-id}/content` | `server.flows.download(id, filepath, include_extract)` |
| 削除 | `DELETE /sites/{site-id}/flows/{flow-id}` | `server.flows.delete(id)` |
| **run** | `POST /sites/{site-id}/flows/{flow-id}/run` | `server.flows.refresh(flow)` |

`publish` の `mode`: `CreateNew` / `Overwrite`。

## Jobs

| 操作 | HTTP | TSC |
|---|---|---|
| 一覧 | `GET /sites/{site-id}/jobs` | `server.jobs.get()` |
| 取得 | `GET /sites/{site-id}/jobs/{job-id}` | `server.jobs.get_by_id(id)` |
| キャンセル | `PUT /sites/{site-id}/jobs/{job-id}` | `server.jobs.cancel(id)` |

`finishCode`: 0=Success, 1=Failed, 2=Cancelled。`completedAt` が null なら未完。

## ページングとフィルタ

| 用途 | TSC |
|---|---|
| ページサイズ | `RequestOptions(pagesize=100)` |
| 名前で絞り込み | `req.filter.add(Filter(Field.Name, Operator.Equals, "stg_orders"))` |
| ソート | `req.sort.add(Sort(Field.CreatedAt, Direction.Desc))` |

## エラーコード対処

| Code | 原因例 | 対処 |
|---|---|---|
| 400 | リクエスト JSON 不正、必須フィールド欠落 | スキーマを公式リファレンスで確認 |
| 401 | 認証失敗（PAT 失効・サイト不一致） | [authentication.md](authentication.md) のトラブルシューティング |
| 403 | 権限不足（サイトロール・プロジェクト権限） | サービスアカウントの権限を見直し |
| 404 | リソース不在 / ID 不正 | LUID と path のスペルを確認 |
| 409 | 既存と衝突（CreateNew で同名 publish 等） | `Overwrite` モードに切り替え |
| 429 | レート制限 | 指数バックオフでリトライ |

## バージョン互換

- `use_server_version=True` を TSC で指定すれば、サーバーが対応する最新版に自動追従
- 手元のサーバーが 2024.1 以前なら REST API バージョン 3.21 以下を明示

## 参考

- [REST API Reference](https://help.tableau.com/current/api/rest_api/en-us/REST/rest_api.htm)
- [tableau-server-client docs](https://tableau.github.io/server-client-python/)
