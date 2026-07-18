---
purpose: tableau-prep-deployer の preflight フェーズが pending segments と dbt 3 レイヤを idempotent に一括作成するアルゴリズム
note: deploy-context.md frontmatter の消費、pending segments の順次作成、dbt 3 レイヤの一括作成、idempotent 性を規定。preflight は goal ≥ ④ の合意で起動 (goal ②/③ では走らせない)
---

# preflight-recipe

`tableau-prep-deployer` の **Preflight フェーズ** の具体手順。[tableau-prep-extractor](../../tableau-prep-extractor/SKILL.md) Phase B が生成した `deploy-context.md` を読み、target までの pending セグメントと dbt 3 レイヤ (`stg / intermediate / marts`) を順に idempotent 作成する。

承認方針: **追加プロンプトは出さない**。preflight (サーバー書込) は **goal ≥ ④ (Cloud publish) のときのみ**走る — session intake の goal 指定が書き込み合意を兼ねる ([autonomous-recovery.md §実行ポリシー](autonomous-recovery.md))。goal ②/③ (ローカル完結) では preflight を起動しない (plan.json の layer LUID は TODO placeholder のまま許容)。スクリプトも非対話。

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

すべての pending を作り切る前提（goal ≥ ④ で target path が確定した時点で全段の作成が同意されている）。`create_project.py` / `create_projects.py` は idempotent なので、すでに存在する project は `[skip]` で安全。

flows/ と datasources/ を分ける理由 (権限分離・一覧性・publish 先の独立) は [../../../../references/project-hierarchy.md](../../../../references/project-hierarchy.md) を参照。

## 完了後 (caller の責務) — Phase B 再実行 (migration-workflow step 4)

preflight で 3 レイヤを作成しても `deploy-context.md` の layer 行 LUID は空のまま — **preflight スクリプトはファイルへ書き戻さない**。caller は preflight 完了後に **tableau-prep-extractor Phase B を再実行** して `deploy-context.md` を更新し、作成済みプロジェクトの LUID を layer 行に埋めてから publish / 後段の decompose へ進む。この更新済み deploy-context が gen_plan_skeleton (plan.json の `flow_projects` / `ds_projects` 充填) と builder Output の projectLuid 供給元になる。順序は migration-workflow の step 2 (Phase B 初回) → step 4 (preflight → Phase B 再実行) → decompose / publish が正。

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
| target LUID 取得不可 | `deploy-context.md` 不整合 / 親プロジェクト削除済 | tableau-prep-extractor Phase B を再実行して `deploy-context.md` を作り直す |
| `create_project.py` が 403 | サービスアカウントの権限不足 | [authentication.md](authentication.md) のサービスアカウント設計を確認 |
| top-level 作成 WARNING に対するユーザー判断 | org governance | 承認/拒否はユーザー側で判断、Skill は強制しない |
