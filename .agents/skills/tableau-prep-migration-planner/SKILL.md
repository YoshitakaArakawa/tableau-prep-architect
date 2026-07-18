---
name: tableau-prep-migration-planner
description: 複数フロー移行または横断工程 (スケジュール設計 / Workbook 参照置換 / PDS backfill) を含む Prep 分解プロジェクトで、scope・移行順・人間作業キュー・進捗を 1 枚に集約する移行計画書 (migration-plan.md + migration-plan.json) を生成し、工程の進行に合わせて更新する Skill。tableau-prep-extractor Phase C の後 (migration-workflow step 3) に骨を作って Stop 1 でユーザー承認を取り、以降は各工程完了時に main agent が status と決定を埋めていく progressive-fill 台帳で、セッション横断の resume state も兼ねる。ユーザーが「移行計画を作って」「計画書を出して」「移行の段取りを整理して」と言ったとき、または対象フローが複数・横断工程 (Q2b = schedule / repoint / backfill) を含むときに起動する。フロー内設計 (命名 / レイヤ / Input policy) には踏み込まない (それは tableau-prep-architect の decomposition-plan が正)。Cloud 副作用なし・ローカルのみ。
---

この skill の正典は [.claude/skills/tableau-prep-migration-planner/SKILL.md](../../../.claude/skills/tableau-prep-migration-planner/SKILL.md)。次の手順で実行する:

1. リポジトリルートの `AGENTS.md` の「Claude Code 記法の読み替え表」を確認する (未読の場合)。
2. 正典 SKILL.md を読み、その手順に従う。frontmatter の Claude Code 固有フィールドは読み替え表に従って解釈する。
3. この skill はユーザー確認ゲート・失敗観測を含むため主会話で実行する。サブエージェントに委譲しない。
