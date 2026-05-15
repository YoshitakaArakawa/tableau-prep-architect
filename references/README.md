# references/

Skill 横断で参照される共通知識。各 Skill の SKILL.md は実行手順だけを持ち、判断基準・構造定義・規約はここを参照する。

## ファイル一覧

| ファイル | 内容 | 主な参照元 |
|---|---|---|
| [input-policy.md](input-policy.md) | Input ノードは Published DS / 仮想接続を指す原則 | prep-architect (analyze, decompose), prep-deployer (publish) |
| [naming-conventions.md](naming-conventions.md) | .tfl ファイル名 / ノード名 / 列名 / Published DS 名の規約 | prep-architect, prep-builder, prep-deployer |
| [tfl-json-schema.md](tfl-json-schema.md) | .tfl/.tflx ファイル形式、flow.json のトップレベル構造、依存関係の罠、新規 .tfl 組み立てパターン | prep-extractor, prep-builder |
| [prep-ui-to-json-mapping.md](prep-ui-to-json-mapping.md) | Tableau Prep UI ステップ ⇔ nodeType ⇔ actions サブタイプの対応表 | prep-extractor, prep-builder |
| [layer-responsibilities.md](layer-responsibilities.md) | dbt 流 stg / int / marts 各レイヤの責務定義と判定基準 | prep-architect (analyze, decompose), prep-builder (配置先決定) |
| [decomposition-plan-format.md](decomposition-plan-format.md) | decompose フェーズが出力する分解設計案の markdown 書式仕様 (architect→builder 契約) | prep-architect (producer), prep-builder (consumer) |
| [project-hierarchy.md](project-hierarchy.md) | Tableau Cloud 上の publish 先構造 (target + dbt 3 レイヤ) の規約 | prep-extractor (Phase B), prep-architect (decompose), prep-deployer (preflight/publish) |

Skill 専用の手順・判断 (1 Skill 内で完結) は各 Skill の `references/` に置く:

- intermediate 分解戦略 → [.claude/skills/prep-architect/references/intermediate-decomposition.md](../.claude/skills/prep-architect/references/intermediate-decomposition.md)
- analysis-report / flow-summary / decomposition-plan の出力書式詳細 → 各 Skill の references/
- build / publish / run / preflight の具体レシピ → 各 Skill の references/

## 編集ガイドライン

- **2 つ以上の Skill が参照する構造定義・規約・契約・パターン集** はここに置く
- **1 Skill 内で完結する手順・判断** は該当 Skill の `references/` に置く
- spec doc から implementation doc への back-reference は書かない (spec は実装非依存に保つ)
- 1 ファイル ≒ 1 トピック。100 行を大きく超える場合は分割を検討
- ファイル末尾の「参考」リンクは最小限。深いネストは避ける (Claude Code の references は one-level-deep が推奨)
- ファイル間で内容が重複したら、片方を真の source とし、もう一方はリンクのみ
