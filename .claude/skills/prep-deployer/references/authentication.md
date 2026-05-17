---
purpose: PAT による Tableau REST API 認証方針と .env 運用ルール
fetched_at: 2026-05-17
note: 認証方式の比較、.env の置き場所と必須変数、PAT 発行手順、失効と再発行のフロー、トラブルシューティングを含む
---

# authentication

`prep-deployer` と `prep-extractor` の認証方針と `.env` 運用ルール。Repo 直下の [../../../../scripts/tableau_auth.py](../../../../scripts/tableau_auth.py) の仕様もここに準ずる。

## なぜ PAT 一択か

| 認証方式 | 使えるか | 備考 |
|---|---|---|
| **Personal Access Token (PAT)** | ✅ | MFA/SSO 環境でも動く。サービスアカウントとの相性◎ |
| Username / Password | ⚠️ 動くが非推奨 | MFA 環境で詰む、平文保存リスク |
| SSO / OAuth | ❌ | REST API のサーバー間認証で SSO はそもそも前提が違う |

**結論**: REST API ベースの prep-deployer では PAT のみサポート。MVP 段階の他の認証方式は実装しない。

## `.env` の配置

| 場所 | 用途 |
|---|---|
| **ユーザー作業フォルダ直下** (`<your-prep-project>/.env`) | 通常の使用ケース。プロジェクトごとに切り替え可能 |
| このリポジトリ直下 (`tableau-prep-architect/.env`) | このリポジトリ自身を開発・テストするとき |
| Skill 内 | **使わない**（自己内包したいケースは現状なし） |

`tableau_auth.find_env_file()` は **現在ディレクトリから祖先方向に最大 6 階層** `.env` を探索する。見つかった最初のものを `load_dotenv()` する。

## 必須環境変数

| 変数 | 例 | 必須 |
|---|---|---|
| `SERVER` | `https://<your-pod>.online.tableau.com` | ✅ |
| `SITE_NAME` | `mysite`（Tableau Cloud のサイトスラッグ。Tableau Server の Default site は空文字） | △ 空可 |
| `PAT_NAME` | `prep-architect-pat` | ✅ |
| `PAT_VALUE` | `xxxxxxx==` | ✅ |

不足時は `tableau_auth.load_credentials()` が `sys.exit` で終了する。

## PAT の発行手順

### Tableau Cloud

1. 右上のユーザーアイコン → **My Account Settings**
2. **Personal Access Tokens** セクション
3. **Token Name** を入力（例: `prep-deployer-pat`）→ **Create new token**
4. 表示された **secret 値を即座にコピー**（一度しか表示されない）
5. `.env` の `PAT_VALUE` に貼り付け

### Tableau Server

ほぼ同じ手順だが、サイト管理者が PAT を有効化している必要あり。詳細は [Tableau 公式: Personal Access Tokens](https://help.tableau.com/current/server/en-us/security_personal_access_tokens.htm)。

## 失効ポリシー

| 環境 | 失効条件 |
|---|---|
| Tableau Cloud | **15 日アクティビティなしで失効** |
| Tableau Server | 管理者の設定次第（既定で無期限〜180 日） |

⚠️ **定期実行が無い CI で PAT を使う場合**、15 日以内に何らかの sign-in が発生するように heartbeat ジョブを置くか、月次でローテーションする運用が必要。

## サービスアカウント設計

PAT は最小権限のサービスアカウント（例: `tableau-deployer@example.com`）で発行する：

| 権限 | 推奨 |
|---|---|
| サイトロール | **Creator**（Prep flow の publish に必須） |
| プロジェクト権限 | 親プロジェクト＋ stg/int/marts のみ |
| MFA | 無効化不要（PAT 経由なので MFA 影響なし） |

## 本番運用での置き換え

`.env` 平文管理は **開発・PoC まで**。本番では:

| 環境 | 推奨先 |
|---|---|
| GitHub Actions | Repository Secrets → `env:` で注入 |
| Azure DevOps | Library Variable Group + Variable Groups |
| AWS | Secrets Manager → 起動時に取得して `os.environ` にセット |
| HashiCorp Vault | KV v2 + agent inject |

いずれも `tableau_auth.py` は変更不要（環境変数として渡れば動く）。

## トラブルシューティング

| エラー | 原因 | 対処 |
|---|---|---|
| `401 Unauthorized` | PAT 失効・無効 | 新しい PAT を発行して `.env` 更新 |
| `401`（直後に再発行したのに） | サイト不一致 | `SITE_NAME` の値を確認（URL の `/site/<ここ>/` 部分） |
| `403 Forbidden` | サービスアカウントの権限不足 | サイトロールを Creator に、プロジェクト権限を付与 |
| `Missing required env vars: PAT_VALUE` | `.env` 読み込めず | `find_env_file()` がパスを返したか stderr で確認 |
