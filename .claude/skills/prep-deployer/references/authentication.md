---
purpose: OAuth (PKCE) による Tableau REST API 認証方針と .env 運用ルール
fetched_at: 2026-05-24
note: 認証方式の選定理由、.env の置き場所と必須変数、OAuth フローの動作、トラブルシューティングを含む
---

# authentication

`prep-deployer` / `prep-extractor` / `prep-pds-augmenter` の認証方針と `.env` 運用ルール。Repo 直下の [../../../../scripts/tableau_auth.py](../../../../scripts/tableau_auth.py) の仕様もここに準ずる。

## なぜ OAuth (PKCE) 一択か

| 認証方式 | 採否 | 備考 |
|---|---|---|
| **OAuth 2.0 Authorization Code + PKCE** | ✅ | 対話的にブラウザでサインイン。`.env` に secret を置かない。MFA/SSO もネイティブに通る |
| Personal Access Token (PAT) | ❌ (本リポでは不採用) | 15 日無アクセスで失効、`.env` に長期 secret が残る。ただし非対話自動化（CI など）が必要になった場合は別途 PAT ベースの簡易スクリプトを切り出す前提 |
| Username / Password | ❌ | MFA 環境で詰む、平文保存リスク |
| Connected App (JWT) | ❌ (本リポでは不採用) | Client ID/Secret 管理が必要、本リポの対話的ユースケースには過剰 |

**結論**: 本リポは個人マシン上で Tableau Prep を対話的に改修するユースケースが前提なので、ブラウザサインイン (OAuth + PKCE) のみサポート。CI/CD で非対話 publish が必要になった場合は、別途 PAT ベースの簡易 REST スクリプトを切り出す。

## OAuth フローの動作概要

`signed_in_server()` を呼ぶと:

1. `<repo>/.auth-cache/session.json` をチェック
   - 同じ `SERVER` / `SITE_NAME` のキャッシュがあれば `GET /api/{version}/sessions/current` で生存確認
   - 200 ならキャッシュの `access_token` をそのまま `_set_auth` に流して **ブラウザ起動なしで完了**（以降の手順 2〜6 スキップ）
   - 401/その他ならキャッシュを破棄して下記に fall through
2. ローカルで callback listener (`http://127.0.0.1:{port}/Callback`) を起動
3. PKCE verifier/challenge を生成、`webbrowser.open()` で Tableau Cloud のサインイン画面を開く
4. ブラウザでサインイン完了 → callback で `code` を受信
5. `POST /oauth2/v1/token` で `code + code_verifier` を交換し `access_token` を取得（3-part: `<id1>|<id2>|<site-luid>`）
6. `access_token` の 3 番目から site LUID を抽出、`GET /api/{version}/sessions/current` で user_id を取得
7. `TSC.Server` インスタンスに `_set_auth(site_luid, user_id, access_token, site_url=SITE_NAME)` で inject
8. `access_token` を `.auth-cache/session.json` に保存（atomic rename + best-effort `chmod 0o600`）
9. 以後、`server.flows.publish(...)` 等は `X-Tableau-Auth: {access_token}` ヘッダで動作
10. **context 抜けで `sign_out` は呼ばない**（次プロセスの cache reuse のため）

CLIENT_ID は UUID を都度生成（OAuth endpoint が要求するため）。Connected App / API Application 等の事前登録は不要。

## session cache の運用

| 項目 | 仕様 |
|---|---|
| 場所 | `<repo>/.auth-cache/session.json`（`.gitignore` 済） |
| 内容 | `server_url` / `site_name` / `access_token` のみ。`user_id` はキャッシュせず読込時に `sessions/current` から取得 |
| マッチキー | `(server_url, site_name)`。site 切替時は自動的に miss → 再 OAuth |
| 寿命 | server 側 TTL（おおむね 2h）に従う。client 側のキャップは設けない |
| 破棄 | `python scripts/tableau_auth.py logout`（server 側 sign_out + ファイル削除） |
| 状態確認 | `python scripts/tableau_auth.py status`（cache の有無 / 生存性を表示） |

**バックアップ / 同期サービスへの注意**: bearer token が含まれるので、`.auth-cache/` をクラウドストレージ（OneDrive / iCloud / Dropbox など）の同期対象に入れないこと。リポを同期フォルダ配下に置く場合は、サービス側で `.auth-cache/` を同期除外する。

## `.env` の配置

| 場所 | 用途 |
|---|---|
| **ユーザー作業フォルダ直下** (`<your-prep-project>/.env`) | 通常の使用ケース |
| このリポジトリ直下 (`tableau-prep-architect/.env`) | このリポジトリ自身を開発・テストするとき |
| Skill 内 | **使わない** |

`tableau_auth.find_env_file()` は **現在ディレクトリから祖先方向に最大 6 階層** `.env` を探索する。見つかった最初のものを `load_dotenv()` する。

## 必須環境変数

| 変数 | 例 | 必須 |
|---|---|---|
| `SERVER` | `https://<your-pod>.online.tableau.com` | ✅ |
| `SITE_NAME` | `mysite`（Tableau Cloud のサイトスラッグ。Default site は空文字） | △ 空可 |
| `OAUTH_CALLBACK_PORT` | `8765` | △ default `8765` |

PAT 系 (`PAT_NAME` / `PAT_VALUE`) は **不要・削除済**。

`SERVER` 不足時は `tableau_auth.load_credentials()` が `sys.exit` で終了する。

## 動作要件

- Python が `webbrowser.open()` 可能な環境（個人 PC で OS のブラウザが起動できること）
- `127.0.0.1:{OAUTH_CALLBACK_PORT}` がローカルで listen 可能（ファイアウォール / 他プロセスとの衝突注意）
- callback まで最大 5 分待機（超過すると `SystemExit`）

## サイトロール

| 権限 | 推奨 |
|---|---|
| サイトロール | **Creator**（Prep flow の publish に必須） |
| プロジェクト権限 | 親プロジェクト＋ stg/int/marts のみ |

OAuth では人間のユーザーアカウントでサインインする想定。サービスアカウント運用が必要なら CI 用 PAT スクリプトを別途用意（本リポのスコープ外）。

## access token の有効期限

Tableau Cloud OAuth access_token はおおむね 1〜2 時間で expire。本リポは **refresh_token を扱わない**（参考実装と同じ設計）。token 期限内で完了しない長時間 job (>2h) はそもそも Prep で動かさない前提。expire 後は `_load_cached_session` の生存確認が 401 を踏んで自動的に再 OAuth に落ちる。

## 複数 fork での挙動

`context: fork` で起動する Skill (prep-builder / prep-deployer / prep-extractor Phase B) は別プロセスだが、`.auth-cache/session.json` を共有するため **最初の 1 fork で OAuth が走ればそれ以降の fork はキャッシュ hit でブラウザ起動なし**。並列 fork が同時に miss を踏んだ場合は両方が再 OAuth を発火しうるが、後勝ちで cache が上書きされるだけで害はない。

## トラブルシューティング

| エラー | 原因 | 対処 |
|---|---|---|
| `timeout waiting for OAuth callback` | 5 分以内にブラウザでサインインが完了しなかった | 再実行。プロキシ / ブラウザ拒否で callback が届かないケースもあり |
| `[Errno 48] Address already in use` (port 8765) | 他プロセスが listener と衝突 | `.env` で `OAUTH_CALLBACK_PORT` を変更 |
| `unexpected access_token shape` | OAuth エンドポイントから期待外のレスポンス | Tableau Cloud 側の障害可能性。時間を置いて再試行 |
| `401 Unauthorized`（サインイン後） | access_token expire (>1〜2h) または server 側 revoke | 再実行で `_load_cached_session` が自動的に miss → ブラウザサインイン。明示破棄したいときは `python scripts/tableau_auth.py logout` |
| `403 Forbidden` | サインインしたユーザーの権限不足 | サイトロールを Creator に、プロジェクト権限を付与 |
| `Missing required env var: SERVER` | `.env` 読み込めず or 未設定 | `find_env_file()` がパスを返したか stderr で確認 |
| `403`（site mismatch） | `SITE_NAME` の値が不一致 | URL の `/site/<ここ>/` 部分を確認 |
