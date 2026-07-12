---
name: prep-schedule-designer
description: 分解後 Prep フロー群 (int/mart) の定期実行スケジュールを設計し、Cloud UI で Linked Task を作成するための設計資料 (schedule-setup-runbook.md + schedule-design.json) を生成する Skill。run-type (Full/Incremental) は decomposed .tfl の実体スキャンで、依存順は LoadSqlProxy スキャンで機械確定し、facts-last の実行順・トリガ (曜日/時刻)・旧スケジュールの削除対象を 1 枚にまとめる。人間の UI セットアップ後は verify モードで設計とサーバー実測 (tasks/linked) を突合する。移行完了後に「スケジュールを設計して」「Linked Task の設計資料を作って」「定期実行を組みたい」「スケジュール設定を検証して」と言われたときに起動。スケジュールの API 作成・旧スケジュール削除はしない (Linked Task は UI 専用)。Cloud は読み取りのみ。
---

この skill の正典は [.claude/skills/prep-schedule-designer/SKILL.md](../../../.claude/skills/prep-schedule-designer/SKILL.md)。次の手順で実行する:

1. リポジトリルートの `AGENTS.md` の「Claude Code 記法の読み替え表」を確認する (未読の場合)。
2. 正典 SKILL.md を読み、その手順に従う。frontmatter の Claude Code 固有フィールドは読み替え表に従って解釈する。
3. この skill は隔離実行 (fork) 前提。サブエージェント (`.codex/agents/` の flow-worker、無ければ既定のサブエージェント) に委譲して実行する。サブエージェントが使えない場合はインライン実行してよいが、出力契約 (成果物はファイルへ書く・主会話へ中間 JSON を流さない・返答は要約 + Timing ブロックのみ) は必ず維持する。
