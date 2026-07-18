---
name: prep-migrate
description: Tableau Prep フロー移行セッションの entry-point 手順書。Session intake (Q1-Q5)・workflow (extract → analyze → decompose → build → publish → run → compare → schedule → repoint)・Stop 1/2 の運用・deploy-context ライフサイクル (preflight → Phase B 再実行) と goal ゲート・失敗時の targeted fix ループを規定し、main agent が各 Skill を正しい順序と goal 段階ゲートで呼び出すための正典。ユーザーが Prep フローの分析 / 分解設計 / 移行 / Cloud publish / E2E 比較 / スケジュール設計 / Workbook repoint / Pulse repoint / backfill を依頼したら、他の作業に入る前にセッション冒頭で必ず起動する。フロー内設計は prep-architect、セッション横断の計画台帳は prep-migration-planner が担い、本 Skill はそれらを呼ぶ順序と intake・停止点のみを持つ。
---

この skill の正典は [.claude/skills/prep-migrate/SKILL.md](../../../.claude/skills/prep-migrate/SKILL.md)。次の手順で実行する:

1. リポジトリルートの `AGENTS.md` の「Claude Code 記法の読み替え表」を確認する (未読の場合)。
2. 正典 SKILL.md を読み、その手順に従う。frontmatter の Claude Code 固有フィールドは読み替え表に従って解釈する。
3. この skill はユーザー確認ゲート・失敗観測を含むため主会話で実行する。サブエージェントに委譲しない。
