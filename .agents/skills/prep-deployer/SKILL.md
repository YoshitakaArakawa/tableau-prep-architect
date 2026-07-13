---
name: prep-deployer
description: prep-builder が生成した .tfl 群を Tableau Server/Cloud に preflight・publish・run する。session intake で goal と target path が合意された前提で承認プロンプトなしに自律実行し、失敗は autonomous-recovery の分類で自動リトライ、回復不能種別 (認証 / 権限 / 容量 / Cloud 障害) は escalation する。.tfl 群が手元に揃っていてサーバーに届けたいとき、publish 済み flow を実行したいとき、ジョブ結果を確認したいとき、「デプロイして」「publish して」「実行して」と言われたときに起動。
---

この skill の正典は [.claude/skills/prep-deployer/SKILL.md](../../../.claude/skills/prep-deployer/SKILL.md)。次の手順で実行する:

1. リポジトリルートの `AGENTS.md` の「Claude Code 記法の読み替え表」を確認する (未読の場合)。
2. 正典 SKILL.md を読み、その手順に従う。frontmatter の Claude Code 固有フィールドは読み替え表に従って解釈する。
3. この skill はユーザー確認ゲート・失敗観測を含むため主会話で実行する。サブエージェントに委譲しない。
