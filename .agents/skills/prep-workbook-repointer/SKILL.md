---
name: prep-workbook-repointer
description: 移行後、旧 Published Data Source を参照する Workbook を新 marts PDS へ差し替えるための設計資料 (repoint-runbook.md + repoint-design.json) を生成し、差し替え後に反映を検証する Skill。左辺 (どの WB がどの旧 PDS を参照するか) は Metadata API の downstreamWorkbooks で、右辺 (旧 PDS → 新 fct PDS の対応) は publish-manifest の source_original_output_name で機械確定し、Tableau Desktop の Replace Data Source で人間が名前選択で差し替えるための対象 WB URL・新旧 PDS 名・手順を 1 枚にまとめる。人間の差し替え後は verify モードでサーバー実測 lineage と突合する。移行完了後に「WB の参照置換」「workbook を新 PDS に差し替え」「repoint の設計資料を作って」「参照置換を検証して」と言われたときに起動。接続の書き換え自体はしない (Replace Data Source は人間の UI 作業)。Cloud は読み取りのみ。
---

この skill の正典は [.claude/skills/prep-workbook-repointer/SKILL.md](../../../.claude/skills/prep-workbook-repointer/SKILL.md)。次の手順で実行する:

1. リポジトリルートの `AGENTS.md` の「Claude Code 記法の読み替え表」を確認する (未読の場合)。
2. 正典 SKILL.md を読み、その手順に従う。frontmatter の Claude Code 固有フィールドは読み替え表に従って解釈する。
3. この skill は隔離実行 (fork) 前提。サブエージェント (`.codex/agents/` の flow-worker、無ければ既定のサブエージェント) に委譲して実行する。サブエージェントが使えない場合はインライン実行してよいが、出力契約 (成果物はファイルへ書く・主会話へ中間 JSON を流さない・返答は要約 + Timing ブロックのみ) は必ず維持する。
