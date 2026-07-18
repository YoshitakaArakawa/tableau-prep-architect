---
name: tableau-prep-builder
description: tableau-prep-architect の decomposition-plan に従って新規 .tfl ファイル群を組み立てる。元 .tfl から該当ノードを抽出し、切れた依存を新規 LoadSqlProxy Input ノード (上流 PDS 参照) に置換、actions レベル分割があれば SuperTransform を分割、末端に Output ノードを追加して zip 化する。ローカル副作用のみで承認不要。decompose 完了後に設計案を実体ある .tfl に落としたいとき、publish 失敗を受けて .tfl を修正したいときに起動。
---

この skill の正典は [.claude/skills/tableau-prep-builder/SKILL.md](../../../.claude/skills/tableau-prep-builder/SKILL.md)。次の手順で実行する:

1. リポジトリルートの `AGENTS.md` の「Claude Code 記法の読み替え表」を確認する (未読の場合)。
2. 正典 SKILL.md を読み、その手順に従う。frontmatter の Claude Code 固有フィールドは読み替え表に従って解釈する。
3. この skill は隔離実行 (fork) 前提。サブエージェント (`.codex/agents/` の flow-worker、無ければ既定のサブエージェント) に委譲して実行する。サブエージェントが使えない場合はインライン実行してよいが、出力契約 (成果物はファイルへ書く・主会話へ中間 JSON を流さない・返答は要約 + Timing ブロックのみ) は必ず維持する。
