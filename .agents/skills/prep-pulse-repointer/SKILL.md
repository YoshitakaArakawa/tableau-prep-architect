---
name: prep-pulse-repointer
description: 移行後、旧 Published Data Source を参照する Tableau Pulse の Metric Definition を新 marts PDS へ差し替える Skill。design (Pulse REST の definitions 全ページ走査 + subscriptions 棚卸しと publish-manifest の突合で対象定義・旧→新 PDS 対応・follower を機械確定し pulse-repoint-runbook.md + pulse-repoint-design.json を生成) / repoint (in-place の datasource 変更は API 不可のため、新 PDS 参照のコピー定義を作成し metric と follower 購読を再作成する自動差し替え。rehearsal コピー → insight 生成の証拠比較 → ユーザー承認 → production の段取りゲート必須) / verify (差し替え後にサーバー実測と突合) の 3 モード。「Pulse の参照置換」「Pulse 定義を新 PDS に差し替え」「Pulse repoint の設計資料を作って」「Pulse repoint を実行して」「Pulse の repoint を検証して」と言われたときに起動。旧定義の削除はしない (連鎖削除があるため人間判断)。サーバー書込は repoint モードの定義/metric/購読作成のみ (design / verify は読み取りのみ)。移行セッション冒頭の intake・goal ゲート・起動順序は references/migration-workflow.md が正典（本 Skill 単体で移行セッションを始めない）。
---

この skill の正典は [.claude/skills/prep-pulse-repointer/SKILL.md](../../../.claude/skills/prep-pulse-repointer/SKILL.md)。次の手順で実行する:

1. リポジトリルートの `AGENTS.md` の「Claude Code 記法の読み替え表」を確認する (未読の場合)。
2. 正典 SKILL.md を読み、その手順に従う。frontmatter の Claude Code 固有フィールドは読み替え表に従って解釈する。
3. この skill は隔離実行 (fork) 前提。サブエージェント (`.codex/agents/` の flow-worker、無ければ既定のサブエージェント) に委譲して実行する。サブエージェントが使えない場合はインライン実行してよいが、出力契約 (成果物はファイルへ書く・主会話へ中間 JSON を流さない・返答は要約 + Timing ブロックのみ) は必ず維持する。
