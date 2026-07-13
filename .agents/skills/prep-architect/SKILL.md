---
name: prep-architect
description: prep-extractor が生成した flow-summary.md を入力に、Tableau Prep の長大フローを dbt 流のレイヤ規律で分析・分解設計する。analyze（現状把握）と decompose（分解設計）の 2 フェーズを、ユーザー指示に応じて順次または個別に実行する。既存の .tfl/.tflx を「分析したい」「分解したい」「dbt 風に再構築したい」「最適化したい」と言われたときに起動。実装（.tfl 生成）は prep-builder、publish 以降は prep-deployer が担当。移行セッション冒頭の intake・goal ゲート・起動順序は prep-migrate が正典（本 Skill 単体で移行セッションを始めない）。
---

この skill の正典は [.claude/skills/prep-architect/SKILL.md](../../../.claude/skills/prep-architect/SKILL.md)。次の手順で実行する:

1. リポジトリルートの `AGENTS.md` の「Claude Code 記法の読み替え表」を確認する (未読の場合)。
2. 正典 SKILL.md を読み、その手順に従う。frontmatter の Claude Code 固有フィールドは読み替え表に従って解釈する。
3. この skill は隔離実行 (fork) 前提。サブエージェント (`.codex/agents/` の flow-worker、無ければ既定のサブエージェント) に委譲して実行する。サブエージェントが使えない場合はインライン実行してよいが、出力契約 (成果物はファイルへ書く・主会話へ中間 JSON を流さない・返答は要約 + Timing ブロックのみ) は必ず維持する。
