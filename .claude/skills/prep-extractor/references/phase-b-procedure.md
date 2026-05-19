# Phase B 実装手順

Phase B (cloud structure extraction) の実装ガイド。SKILL.md からは 1 行サマリでしか参照されないため、本ファイルに詳細を集約する。

## モデル: target と任意深さの上位階層

publish 先構造は **「最下層は規約固定、それより上は柔軟」** とする:

```
<top-level>                  ┐
└── (任意の中間階層 0個以上)   │ ← この上位パスはユーザーごとに自由
    └── target               ┘   target = stg/int/marts の直上 (= publish 先プロジェクト群の親)
        ├── stg/             ┐
        ├── intermediate/    │ ← この 3 つは規約固定、prep-deployer が承認付き作成
        └── marts/           ┘
```

| 階層 | 例 | 責務 |
|---|---|---|
| **dbt layers** (固定) | `stg / intermediate / marts` | prep-deployer が承認付き作成 |
| **target** | `flow241407_decompose` / `Sales Analytics` / `v1` | publish 先 dbt 3 つの直上。存在しなくても良い |
| **上位の中間階層** | `99_Sandbox/Q4-2026/...` 等、ユーザー依存 | 存在するものは尊重、不足分は prep-deployer が承認付き作成 |

ユーザーが指定する path は **target のフルパス**（target までの全セグメント）。深さは何段でも良い:

- `"Sales Analytics"` — top-level プロジェクトを target に
- `"99_Sandbox/flow241407_decompose"` — sandbox 1 段 + target
- `"99_Sandbox/Q4-2026/decompose-X/v1"` — 中間 2 段 + target
- LUID 直指定もあり

`get_project_structure.py` は path を walk し、**存在する prefix（`existing_chain`）** と **作成すべき残り（`pending_segments`）** に分割する。後段の prep-deployer はそれをループで埋める。

自然言語による path 指示 (例: 「99_Sandbox の下に decompose 用のフォルダを作って」) の path 化は **caller (メインエージェント) の責務**。本 Skill は確定済み path のみ受ける ([CLAUDE.md](../../../../CLAUDE.md) Session intake Q4 補足参照)。

## 入力

| 入力 | 扱い |
|---|---|
| target path（深さ自由、例: `"99_Sandbox/Q4-2026/flow241407_decompose"`） | top-level から `parent_id` チェーンを walk。途中で見つからないセグメントは pending |
| または target LUID | `server.projects.get_by_id` で直接取得、parent chain を逆走して existing prefix を再構成 |
| `.env`（Repo 直下 or ユーザー作業フォルダ） | `SERVER`, `SITE_NAME`, `PAT_NAME`, `PAT_VALUE` |

加えて出力先 `deploy-context.md` のパス。

## 出力 (`deploy-context.md`)

frontmatter:

```yaml
target_path: 99_Sandbox/Q4-2026/decompose-X/v1
target_status: exists | pending
target_luid: <luid or null>
existing_prefix_path: 99_Sandbox       # 最深の既存セグメント。null = 全部 pending（top-level から）
existing_prefix_luid: <luid or null>
pending_segments:                      # 作成すべきセグメント列。target=existing なら []
  - Q4-2026
  - decompose-X
  - v1
```

本文セクション:

1. **Target (parent of stg/int/marts)** — path / LUID / status / writeable?
2. **Existing prefix** — 既存の最深 path と chain（root→leaf テーブル）
3. **Pending segments** — 作成順テーブル（`parent at creation time` 列付き）
4. **Subprojects directly under target** — target 直下（exists のときのみ）
5. **dbt layer presence** — `stg / intermediate / marts` 有無
6. **Existing flows in target subtree** — 名前衝突回避用
7. **Next step** — prep-architect / prep-deployer への引き渡し

## 手順

```bash
# target が既存
python .claude/skills/prep-extractor/scripts/get_project_structure.py \
    --project-path "Sales Analytics" \
    -o work/<date>/reports/deploy-context.md

# 標準的なネスト 1 段、target は未作成
python .claude/skills/prep-extractor/scripts/get_project_structure.py \
    --project-path "99_Sandbox/flow241407_decompose" \
    -o work/<date>/reports/deploy-context.md

# 深いネスト、中間も未作成
python .claude/skills/prep-extractor/scripts/get_project_structure.py \
    --project-path "99_Sandbox/Q4-2026/decompose-X/v1" \
    -o work/<date>/reports/deploy-context.md

# LUID 直指定
python .claude/skills/prep-extractor/scripts/get_project_structure.py \
    --project-id <luid> \
    -o work/<date>/reports/deploy-context.md
```

スクリプトは:

1. 全プロジェクトを `server.projects.get()` で fetch（pagesize=1000、ページング対応）
2. path を `/` で分割し、top-level → leaf に向かって **1 セグメントずつ** `(parent_id, name)` で照合
3. 最初に存在しなかったセグメントとそれ以降を `pending_segments` に積む
4. ambiguity（同名複数）は ValueError（`--project-id` で解消）
5. target が存在すれば直下サブプロジェクトと subtree 内の flow を集計
6. frontmatter + sections を組み立てて Write

## 制約 (Phase B)

- 読み取り専用 — サブプロジェクト作成や権限変更は **prep-deployer の preflight** に委譲
- `writeable` フィールドは TSC が PAT によっては populate しないため `unknown` で報告するケースあり（実体は publish 試行で確認）
- 同名 top-level プロジェクトが複数ある site では `--project-id` で曖昧性解消が必要
