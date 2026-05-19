---
purpose: 新規 scripts / references ファイルの配置判断ルール (どこに置くか)
fetched_at: 2026-05-17
note: 構造説明書ではなく判断ルール集。ディレクトリ全体像は ls で確認 (tree 図は drift するため維持しない)
---

# repo-layout

新しい script や reference ドキュメントを **どこに置くべきか** の判断ルール。ディレクトリ実体は `ls` / `tree` で確認できる (図はここに置かない、drift するため)。

## 配置ルール

| 場所 | 入る対象 | 例 |
|---|---|---|
| repo 直下 `scripts/` | **2 つ以上の Skill が import / 呼び出す** 共通モジュールまたは orchestrator | `tableau_auth.py` (認証), `flow_io.py` (.tfl IO), `publish_manifest.py` (manifest 読み書き), `run_layer.py` (同一レイヤの flow 群を server-side parallel で run) |
| `.claude/skills/<skill>/scripts/` | **その Skill 専用、外から呼ばれない** | prep-extractor の `inspect_actions.py` / `get_project_structure.py`、prep-deployer の `publish_flow.py` / `create_project.py` 等 |
| repo 直下 `references/` | **2 つ以上の Skill が参照する共通規約・スキーマ・ポリシー** | `input-policy.md`, `naming-conventions.md`, `tfl-json-schema.md`, `project-hierarchy.md`, 本ファイル |
| `.claude/skills/<skill>/references/` | **その Skill 専用のレシピ・フォーマット仕様** | `flow-summary-format.md`, `analysis-report-format.md`, `build-recipe.md`, `preflight-recipe.md` |

判断基準: **2 つ以上で使うなら repo 直下、単一 Skill 内で完結するなら Skill 配下**。

## 昇格 (Skill 配下 → repo 直下)

ある Skill 配下のファイルを別 Skill も使いたくなったら repo 直下に **昇格** する。逆向き (repo 直下 → Skill 配下) は基本ない。

昇格手順:
1. ファイルを移動
2. 元のパスを import / 参照している箇所を全て更新
3. 旧パスに「moved to ../...」のような転送 stub は **置かない** (clean break)

## 主要ディレクトリ (補足)

`ls` で十分だが、判断ルールと紐付けて理解したい場合の補足:

- `CLAUDE.md` — project memory / workflow / 規約 (常時ロード)
- `references/` — Skill 横断の共通知識 (本ファイル含む)
- `scripts/` — Skill 横断の共通モジュール
- `.claude/skills/{prep-extractor,prep-architect,prep-builder,prep-deployer}/` — 各 Skill 本体
- `work/` — セッション作業フォルダ (git 追跡外、詳細は [CLAUDE.md §work/ ディレクトリ規約](../CLAUDE.md#work-ディレクトリ規約))
