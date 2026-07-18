---
name: tableau-prep-extractor
description: Tableau Prep の .tfl / .tflx / flow.json およびサーバー上のプロジェクト階層を読み、後段が直接 JSON / REST を見なくて済むコンパクトなサマリに再構成する Skill。Phase A = flow extraction（flow-summary.md）、Phase B = cloud context extraction（deploy-context.md + input-dispatch-mech.json）、Phase C = flow dependency mapping（flow-dependencies.md、複数フロー移行の計画時のみ）の 3 フェーズを持つ。大きな Prep フロー（数十〜数百ノード）を解析・分解する前、または Tableau Cloud に publish する前に必ず実行する。ユーザーが「フローを extract して」「flow-summary を作って」「publish 先のプロジェクトを確認して」「Input を分類して」「フロー間の依存を調べて」「移行順を決めて」と言ったとき、サーバー上のフローを DL したいときに起動（list_flows.py / download_flow.py）。移行セッション冒頭の intake・goal ゲート・起動順序は references/migration-workflow.md が正典（本 Skill 単体で移行セッションを始めない）。
---

この skill の正典は [.claude/skills/tableau-prep-extractor/SKILL.md](../../../.claude/skills/tableau-prep-extractor/SKILL.md)。次の手順で実行する:

1. リポジトリルートの `AGENTS.md` の「Claude Code 記法の読み替え表」を確認する (未読の場合)。
2. 正典 SKILL.md を読み、その手順に従う。frontmatter の Claude Code 固有フィールドは読み替え表に従って解釈する。
3. この skill は隔離実行 (fork) 前提。サブエージェント (`.codex/agents/` の flow-worker-lite、無ければ既定のサブエージェント) に委譲して実行する。サブエージェントが使えない場合はインライン実行してよいが、出力契約 (成果物はファイルへ書く・主会話へ中間 JSON を流さない・返答は要約 + Timing ブロックのみ) は必ず維持する。
