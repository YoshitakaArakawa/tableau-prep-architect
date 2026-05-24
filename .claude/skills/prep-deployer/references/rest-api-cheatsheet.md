---
source: https://help.tableau.com/current/api/rest_api/en-us/REST/rest_api.htm
fetched_at: 2026-05-17
source_last_known_update: 不明（Tableau Cloud 2024.x – 2026.x を想定）
note: prep-deployer が使う主要エンドポイント（Auth / Projects / Flows / Jobs）の生 HTTP + TSC 両方の早見表。リクエスト・レスポンスの XML/JSON 例を含む
---

# rest-api-cheatsheet

`prep-deployer` が使う Tableau REST API の主要エンドポイント早見表。`tableauserverclient` (TSC) と生 HTTP の両方で記載。バージョンは Tableau Cloud 2024.x – 2026.x を想定。

## 認証

### Sign In（OAuth 2.0 Authorization Code + PKCE）

本リポは PAT を使わず、ブラウザサインインの OAuth で `access_token` を取得する。フローの詳細は [authentication.md](authentication.md) 参照。要旨:

1. `POST /oauth2/v1/auth?...` をブラウザで開く（PKCE challenge を query に含む）
2. ローカル callback (`http://127.0.0.1:{port}/Callback`) で `code` を受信
3. `POST /oauth2/v1/token` で `code + code_verifier` を交換 → `access_token` (3-part: `<id1>|<id2>|<site-luid>`)
4. `GET /api/{version}/sessions/current` で user_id を取得
5. 以降のリクエストは `X-Tableau-Auth: {access_token}` ヘッダーで叩く

### 共通ヘルパ経由

```python
from tableau_auth import signed_in_server

with signed_in_server() as server:
    # TSC.Server は access_token が inject 済み
    flows, _ = server.flows.get()
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
| 401 | 認証失敗（access token 失効・サイト不一致） | [authentication.md](authentication.md) のトラブルシューティング |
| 403 | 権限不足（サイトロール・プロジェクト権限） | サインインしたユーザーの権限を見直し |
| 404 | リソース不在 / ID 不正 | LUID と path のスペルを確認 |
| 409 | 既存と衝突（CreateNew で同名 publish 等） | `Overwrite` モードに切り替え |
| 429 | レート制限 | 指数バックオフでリトライ |

## バージョン互換

- `use_server_version=True` を TSC で指定すれば、サーバーが対応する最新版に自動追従
- 手元のサーバーが 2024.1 以前なら REST API バージョン 3.21 以下を明示

## 参考

- [REST API Reference](https://help.tableau.com/current/api/rest_api/en-us/REST/rest_api.htm)
- [tableau-server-client docs](https://tableau.github.io/server-client-python/)
