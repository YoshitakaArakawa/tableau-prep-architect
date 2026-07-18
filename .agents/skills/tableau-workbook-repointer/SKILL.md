---
name: tableau-workbook-repointer
description: 移行後、旧 Published Data Source を参照する Workbook を新 marts PDS へ差し替える Skill。design (Metadata API の downstreamWorkbooks と publish-manifest の突合で対象 WB・旧→新 PDS 対応を機械確定し repoint-runbook.md + repoint-design.json を生成) / repoint (TWB を DL して content_url と表示名を書き換え republish する自動差し替え。リハーサル publish → 証拠比較 → ユーザー承認 → 本番 Overwrite の段取りゲート必須) / verify (差し替え後にサーバー実測 lineage と突合) の 3 モード。「WB の参照置換」「workbook を新 PDS に差し替え」「repoint の設計資料を作って」「repoint を実行して」「自動で差し替えて」「参照置換を検証して」と言われたときに起動。差し替えの既定経路は repoint モードで、手術不可ケース・権限制約時のみ Desktop の Replace Data Source による人間差し替えに runbook で fallback する。サーバー書込は repoint モードの WB republish のみ (design / verify は読み取りのみ)。移行セッション冒頭の intake・goal ゲート・起動順序は references/migration-workflow.md が正典（本 Skill 単体で移行セッションを始めない）。
---

この skill の正典は [.claude/skills/tableau-workbook-repointer/SKILL.md](../../../.claude/skills/tableau-workbook-repointer/SKILL.md)。次の手順で実行する:

1. リポジトリルートの `AGENTS.md` の「Claude Code 記法の読み替え表」を確認する (未読の場合)。
2. 正典 SKILL.md を読み、その手順に従う。frontmatter の Claude Code 固有フィールドは読み替え表に従って解釈する。
3. この skill は隔離実行 (fork) 前提。サブエージェント (`.codex/agents/` の flow-worker、無ければ既定のサブエージェント) に委譲して実行する。サブエージェントが使えない場合はインライン実行してよいが、出力契約 (成果物はファイルへ書く・主会話へ中間 JSON を流さない・返答は要約 + Timing ブロックのみ) は必ず維持する。
