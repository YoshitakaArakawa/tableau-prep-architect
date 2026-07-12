---
name: prep-pds-augmenter
description: Tableau Cloud / Server 上の Published Data Source を Calculated Field 注入と column-level transforms (rename / cast / hide) で機械的に改変・量産する Skill。source は extract (Hyper-backed) / live (既存 Live PDS) / vconn (仮想接続から base .tds をゼロ合成) の 3 種で、.tds XML 編集 → publish → 再 DL 検証を一気通貫で実行する。Prep フローが publish した Hyper Output に派生列を足したいとき、分解元 Prep の vconn Input から stg 相当の Live PDS を publish したいとき、既存 Live PDS に BI 向けの rename / cast / hide を当てたいとき、「PDS に calc を注入して」「calc 込み PDS を量産して」と言われたときに起動。calc / transform 仕様と vconn 列メタは caller が明示提供する前提で auto-detect しない。
---

この skill の正典は [.claude/skills/prep-pds-augmenter/SKILL.md](../../../.claude/skills/prep-pds-augmenter/SKILL.md)。次の手順で実行する:

1. リポジトリルートの `AGENTS.md` の「Claude Code 記法の読み替え表」を確認する (未読の場合)。
2. 正典 SKILL.md を読み、その手順に従う。frontmatter の Claude Code 固有フィールドは読み替え表に従って解釈する。
3. この skill はユーザー確認ゲート・失敗観測を含むため主会話で実行する。サブエージェントに委譲しない。
