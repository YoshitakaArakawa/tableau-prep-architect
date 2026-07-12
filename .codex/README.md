# .codex/

OpenAI Codex 向けのプロジェクト単位設定です。Skill の正典は `.claude/skills/` のままで、ここは Codex から使うための入口だけを置きます。

## 中身

- `config.toml` — Tableau MCP サーバー構成のテンプレート (既定は全行コメントアウト) と trust ゲートの説明。
- `agents/flow-worker.toml` — fork 系 Skill (extract / analyze / decompose / build / compare 等) を隔離実行するサブエージェント定義。
- `agents/flow-worker-lite.toml` — prep-extractor 用の軽量版 (低 reasoning effort)。

## trust しないと無視される

`.codex/` 配下 (config.toml / agents/) は **プロジェクトが trusted のときだけ** Codex に読み込まれます。untrusted では黙って無視され、MCP 構成もサブエージェント定義も効きません。

## 有効化手順

1. ユーザーの Codex 設定 (`~/.codex/config.toml`) で、このリポジトリのクローン先を trusted に登録する:

   ```toml
   [projects."/path/to/tableau-prep-architect"]
   trust_level = "trusted"
   ```

   (パスはプレースホルダ。各自の絶対パスに置き換える。)
2. Tableau MCP を使う場合は `config.toml` の `[mcp_servers.tableau]` テンプレートのコメントを外し、自分の環境の Tableau MCP 実装に合わせて埋める。
3. 認証まわり (`SERVER` / `SITE_NAME`) は Claude Code と共通の `.env` を使う (`.env.template` 参照)。

セットアップ全体はリポジトリルートの `README.md`「Codex で使う」節を参照してください。
