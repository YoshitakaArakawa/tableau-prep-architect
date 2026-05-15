---
purpose: Tableau Cloud 上での publish 先構造の規約。dbt 流レイヤを最下層に固定しつつ、その上の階層はユーザー文脈に応じて柔軟にする方針
fetched_at: 2026-05-17
note: target (= stg/int/marts の直上) と pending segments モデル、idempotent 作成、権限指針、top-level 作成の注意
---

# project-hierarchy

Tableau Server/Cloud 上で `prep-deployer` が publish 先として扱うプロジェクト階層の規約。

## モデル: 最下層固定 + 上位柔軟

```
<top-level>                  ┐
└── (任意の中間階層 0個以上)   │ ← 「上位構造」、ユーザー文脈次第
    └── target               ┘   target = stg/int/marts の直上
        ├── stg/             ┐
        ├── intermediate/    │ ← 「dbt レイヤ」、規約固定
        └── marts/           ┘
```

- **dbt レイヤ** (`stg / intermediate / marts`): 必ずこの 3 つ、必ず target の直下。dbt の `models/{staging,intermediate,marts}/` と一対一対応
- **target**: 上記 3 つの **直上のプロジェクト**。「分解作業 1 件の単位」または「既存の分析プロジェクト全体の親」など、ユーザー文脈で意味合いが変わる
- **上位構造**: target の上は任意の深さ・任意の命名。組織のサンドボックスエリア (`99_Sandbox`)、四半期分割 (`Q4-2026`)、チーム名、バージョン (`v1`) など、何でも入れて良い

ユーザーは **target のフルパス** を指定する（例 `"99_Sandbox/Q4-2026/flow241407_decompose"`）。上位の中間階層も target 自身も、未作成なら preflight が承認付きで埋める。

## preflight の挙動

`prep-deployer` の preflight が pending segments と dbt 3 レイヤを idempotent に作成する。アルゴリズム・承認方針・エラー時挙動は [prep-deployer/references/preflight-recipe.md](../.claude/skills/prep-deployer/references/preflight-recipe.md) に集約 (SSOT)。本ファイルはモデル定義のみを扱う。

## スクリプト

### `create_project.py`(1 セグメントずつ)

```bash
# 既存親の下に作る（pending loop 用）
python create_project.py --parent-path "99_Sandbox" --name "flow241407_decompose"
python create_project.py --parent-id <luid>          --name "Q4-2026"

# top-level に作る（stderr に WARNING を出すが処理は止めない）
python create_project.py --name "new-top-level-folder"
```

- 既存なら `[skip]`、新規なら `[created]` をログ
- 常に非対話 (session intake の target path 指定が合意、[autonomous-execution-policy.md](../.claude/skills/prep-deployer/references/autonomous-execution-policy.md))
- 親未指定 (= top-level 作成) の場合は stderr に WARNING を出力 (governance 上の事後監査用、処理は止めない)

### `create_projects.py`(dbt 3 レイヤをまとめて)

```bash
# target name で指定
python create_projects.py --parent-name "flow241407_decompose"

# target LUID で指定（preflight からはこちらを使う、確実）
python create_projects.py --parent-id <target-luid>

# 一部のレイヤだけ作る
python create_projects.py --parent-id <target-luid> --layers stg,marts
```

ルール:

1. target 配下の既存プロジェクト名一覧を取得
2. 名前が `stg` / `intermediate` / `marts` のいずれかに一致するものは `[skip]`
3. 存在しないものだけ `POST /projects` で作成

target 自体が存在しない場合は `create_projects.py` を呼ぶ前に `create_project.py` で先に作る。

## 推奨権限テンプレ

`create_project.py` / `create_projects.py` は `ManagedByOwner` で作成するだけで、具体的な権限割当は **行わない**（ロール設計が組織依存のため）。デプロイ後に手動 or 別スクリプトで以下のテンプレを適用することを推奨:

| サブプロジェクト | ETL チーム | BI チーム | 一般ユーザー |
|---|---|---|---|
| `stg/` | Editor | Viewer | None |
| `intermediate/` | Editor | None | None |
| `marts/` | None | Editor | Viewer |

理由:
- `stg/` は ETL チームが書き換え、BI チームは中身を確認できる
- `intermediate/` はビジネスロジックの内部品。直接公開しない
- `marts/` は分析結果の保管庫。BI チームが管理し、Viewer に広く公開

target 自体（および上位中間階層）の権限は組織ガバナンスに従う。preflight では設定しない。

## `contentPermissions` の選択

`ManagedByOwner` を採用:

| モード | 挙動 |
|---|---|
| **`ManagedByOwner`** | 子プロジェクト・子コンテンツの権限は各オーナーが管理 |
| `LockedToProject` | 子コンテンツの権限はプロジェクト権限と同一に強制 |

`LockedToProject` の方が運用ガバナンスは強いが、複数プロジェクト跨ぎでの DS 参照（例: rpt が fct/dim Published DS を Input にする）を想定する場合は柔軟性が下がる。MVP は `ManagedByOwner`。組織のニーズに応じて変更可。

## top-level 作成についての注意

`create_project.py` は `--parent-*` 引数を省略すると **top-level プロジェクトの作成** を試みる。これは:

- 組織ガバナンス上のインパクトが大きい（命名規則、権限、責任者の明確化が必要）
- 誤って `99_Sandbox` のような「サンドボックス領域」を乱造するリスク
- AI Agent が安易に呼ばないよう、スクリプトが stderr に WARNING を出す

```
WARNING: creating top-level project '<name>' — org governance implications. Audit after the fact.
[created] '<name>' at top-level
  LUID: ...
```

session intake (CLAUDE.md step 0 Q4) で top-level を含む target path が指示されていれば、処理は止めずに WARNING を残して進む。後段で governance 上の事後監査ができるよう stderr に出すのが目的 ([autonomous-execution-policy.md](../.claude/skills/prep-deployer/references/autonomous-execution-policy.md))。

## ambiguity（同名複数）

同名のプロジェクトが同じ親の下に複数あるケース（rare だが Tableau Cloud では可能）:

- `get_project_structure.py` / `create_project.py` ともに ValueError / `ERROR` で停止
- ユーザーに `--project-id` / `--parent-id` での LUID 指定を促す

## 例外ケース

### target = top-level

例: `--project-path "Sales Analytics"` で `Sales Analytics` が既存 top-level プロジェクトの場合。

→ `existing_chain = [Sales Analytics]`, `pending_segments = []`, `target_status = exists`。preflight は dbt レイヤ作成のみ実行。

### 全部 pending（top-level から作る）

例: `--project-path "BrandNewTopLevel/work-1"` で BrandNewTopLevel すらない場合。

→ `existing_chain = []`, `pending_segments = ["BrandNewTopLevel", "work-1"]`。preflight は top-level 作成 WARNING → 中間 → dbt 3 レイヤの順で 4 回承認を取る。

### 既存 prefix + 数段 pending

例: `99_Sandbox` だけ存在で `--project-path "99_Sandbox/Q4-2026/decompose-X/v1"`。

→ `existing_chain = [99_Sandbox]`, `pending_segments = ["Q4-2026", "decompose-X", "v1"]`。preflight は 3 + 1 = 4 回の承認。
