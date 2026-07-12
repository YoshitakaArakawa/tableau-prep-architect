---
name: prep-output-comparator
description: 元フローの最終 Published DS と分解後フローの最終 Published DS を Tableau Metadata API + Tableau MCP で比較し、列差分と全体行数差分の機械的差分を Markdown レポートとして出力する Skill。prep-deployer の publish/run 完了後に「分解後 DS が元と等価か」の基礎的な parity チェックをしたいとき、ユーザーが「E2E 比較して」「元と新で差分を確認して」「parity チェックして」と発言したときに起動する。原因分析・修正提案・値そのものの比較は持たない (値同値性が必要なら caller が個別に query-datasource を叩くか、本 Skill を fork して拡張する)。修正判断はメインエージェントが Markdown を読んで prep-builder / prep-deployer の再呼び出しで対応する。
---

この skill の正典は [.claude/skills/prep-output-comparator/SKILL.md](../../../.claude/skills/prep-output-comparator/SKILL.md)。次の手順で実行する:

1. リポジトリルートの `AGENTS.md` の「Claude Code 記法の読み替え表」を確認する (未読の場合)。
2. 正典 SKILL.md を読み、その手順に従う。frontmatter の Claude Code 固有フィールドは読み替え表に従って解釈する。
3. この skill は隔離実行 (fork) 前提。サブエージェント (`.codex/agents/` の flow-worker、無ければ既定のサブエージェント) に委譲して実行する。サブエージェントが使えない場合はインライン実行してよいが、出力契約 (成果物はファイルへ書く・主会話へ中間 JSON を流さない・返答は要約 + Timing ブロックのみ) は必ず維持する。
