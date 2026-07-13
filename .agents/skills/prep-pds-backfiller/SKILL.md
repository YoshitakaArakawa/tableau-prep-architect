---
name: prep-pds-backfiller
description: 分解後の incremental accumulator PDS に、旧 output PDS の累積履歴を hyper-level surgery で一度だけ seed する (backfill) Skill。deployed flow から accumulator を解決し、列整合検証 → schedule interlock → snapshot 退避 → dry-run → sandbox preview → 明示承認後の本番 Overwrite → 受け入れ incremental 1 サイクル → schedule 再開 を段階実行する。seam (baseline より前の履歴のみ挿入) と replace (sentinel を捨てて全ロード) の 2 モードを持つ。「backfill して」「旧 PDS の履歴を新 PDS に移して」「履歴を seed して」と言われたとき、incremental フロー分解後に過去履歴が欠けているときに起動。値比較・parity 判定は持たない (prep-output-comparator に委譲)。
---

この skill の正典は [.claude/skills/prep-pds-backfiller/SKILL.md](../../../.claude/skills/prep-pds-backfiller/SKILL.md)。次の手順で実行する:

1. リポジトリルートの `AGENTS.md` の「Claude Code 記法の読み替え表」を確認する (未読の場合)。
2. 正典 SKILL.md を読み、その手順に従う。frontmatter の Claude Code 固有フィールドは読み替え表に従って解釈する。
3. この skill はユーザー確認ゲート・失敗観測を含むため主会話で実行する。サブエージェントに委譲しない。
