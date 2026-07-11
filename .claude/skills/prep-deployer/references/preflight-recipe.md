---
purpose: prep-deployer の preflight フェーズが pending segments と dbt 3 レイヤを idempotent に一括作成するアルゴリズム
note: deploy-context.md frontmatter の消費、pending segments の順次作成、dbt 3 レイヤの一括作成、idempotent 性を規定。承認は step 0 の target path 指定で兼ねる
---

# preflight-recipe

`prep-deployer` の **Preflight フェーズ** の具体手順。[prep-extractor](../../prep-extractor/SKILL.md) Phase B が生成した `deploy-context.md` を読み、target までの pending セグメントと dbt 3 レイヤ (`stg / intermediate / marts`) を順に idempotent 作成する。

承認方針: **追加プロンプトは出さない**。session intake で target path が明示されたことが合意 ([autonomous-recovery.md §実行ポリシー](autonomous-recovery.md))。スクリプトも非対話。

階層モデルの全体像は [../../../../references/project-hierarchy.md](../../../../references/project-hierarchy.md) を参照。

## アルゴリズム

```
deploy-context.md の frontmatter から:
  existing_prefix_luid, pending_segments, target_status を取得

# Step 1: 上位 pending segments を作って target まで到達
parent_luid = existing_prefix_luid      # null なら top-level
for seg in pending_segments:
    create_project.py --parent-id <parent_luid> --name <seg>
    parent_luid = 返ってきた LUID
target_luid = parent_luid

# Step 2: target 直下に flows/ と datasources/ を作る (新レイアウト)
flows_luid       = create_project.py --parent-id <target_luid> --name flows
datasources_luid = create_project.py --parent-id <target_luid> --name datasources

# Step 3: flows/ と datasources/ それぞれの下に dbt 3 レイヤを作る
missing_flows = [stg/int/marts のうち flows/ 配下に存在しないもの]
if missing_flows:
    create_projects.py --parent-id <flows_luid> --layers <missing_flows>

missing_ds = [stg/int/marts のうち datasources/ 配下に存在しないもの]
if missing_ds:
    create_projects.py --parent-id <datasources_luid> --layers <missing_ds>
```

すべての pending を作り切る前提（ユーザーが target path を指示した時点で全段の作成が同意されている）。`create_project.py` / `create_projects.py` は idempotent なので、すでに存在する project は `[skip]` で安全。

flows/ と datasources/ を分ける理由 (権限分離・一覧性・publish 先の独立) は [../../../../references/project-hierarchy.md](../../../../references/project-hierarchy.md) を参照。

## エラー時の挙動

- `create_project.py` が 403 / 404 等で失敗した場合、その時点で停止しユーザーに報告。idempotent なので作成済み分はそのまま残し、原因解消後に再 preflight で続きから作成
- top-level 作成（`existing_prefix` が null）は `create_project.py` 側で **WARNING を出す**（org governance の都合）。承認は取らないが、ユーザー目視で気付けるよう ログ出力する

## スクリプト

| スクリプト | 用途 |
|---|---|
| `scripts/create_project.py` | 1 セグメントずつ作成 (pending loop で繰り返し呼ぶ。target 直下の `flows/` と `datasources/` も本スクリプトで作成) |
| `scripts/create_projects.py` | dbt 3 レイヤをまとめて作成 (flows/ 配下と datasources/ 配下の **2 回呼ぶ**) |

両方とも idempotent かつ非対話。重複呼び出しは `[skip]` で安全。

## 失敗時の戻り先

| 発覚 | 想定原因 | 戻り先 |
|---|---|---|
| target LUID 取得不可 | `deploy-context.md` 不整合 / 親プロジェクト削除済 | prep-extractor Phase B を再実行して `deploy-context.md` を作り直す |
| `create_project.py` が 403 | サービスアカウントの権限不足 | [authentication.md](authentication.md) のサービスアカウント設計を確認 |
| top-level 作成 WARNING に対するユーザー判断 | org governance | 承認/拒否はユーザー側で判断、Skill は強制しない |
