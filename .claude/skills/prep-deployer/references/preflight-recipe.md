---
purpose: prep-deployer の preflight フェーズが pending segments と dbt 3 レイヤを idempotent に一括作成するアルゴリズム
fetched_at: 2026-05-17
note: deploy-context.md frontmatter の消費、pending segments の順次作成、dbt 3 レイヤの一括作成、idempotent 性を規定。承認は step 0 の target path 指定で兼ねる
---

# preflight-recipe

`prep-deployer` の **Preflight フェーズ** の具体手順。[prep-extractor](../../prep-extractor/SKILL.md) Phase B が生成した `deploy-context.md` を読み、target までの pending セグメントと dbt 3 レイヤ (`stg / intermediate / marts`) を順に idempotent 作成する。

承認方針: **追加プロンプトは出さない**。session intake (CLAUDE.md step 0) で target path がユーザーから明示されていることが preflight 全体の合意。スクリプトも非対話。承認ポリシー全体は [autonomous-execution-policy.md](autonomous-execution-policy.md) を参照 (preflight だけでなく publish / run も同じ「session intake で合意を取り切る」モデル)。

階層モデルの全体像は [../../../../references/project-hierarchy.md](../../../../references/project-hierarchy.md) を参照。

## アルゴリズム

```
deploy-context.md の frontmatter から:
  existing_prefix_luid, pending_segments, target_status を取得

parent_luid = existing_prefix_luid      # null なら top-level
for seg in pending_segments:            # 順次作成
    create_project.py --parent-id <parent_luid> --name <seg>
    parent_luid = 返ってきた LUID

# parent_luid は今や target の LUID
target_luid = parent_luid

dbt layer presence を確認:
    missing = [stg/int/marts のうち存在しないもの]
    if missing:
        create_projects.py --parent-id <target_luid> --layers <missing>
```

すべての pending を作り切る前提（ユーザーが target path を指示した時点で全段の作成が同意されている）。

## エラー時の挙動

- `create_project.py` が 403 / 404 等で失敗した場合、その時点で停止しユーザーに報告。idempotent なので作成済み分はそのまま残し、原因解消後に再 preflight で続きから作成
- top-level 作成（`existing_prefix` が null）は `create_project.py` 側で **WARNING を出す**（org governance の都合）。承認は取らないが、ユーザー目視で気付けるよう ログ出力する

## スクリプト

| スクリプト | 用途 |
|---|---|
| `scripts/create_project.py` | 1 セグメントずつ作成（pending loop で繰り返し呼ぶ） |
| `scripts/create_projects.py` | dbt 3 レイヤをまとめて作成（最後の 1 回） |

両方とも idempotent かつ非対話。重複呼び出しは `[skip]` で安全。

## 失敗時の戻り先

| 発覚 | 想定原因 | 戻り先 |
|---|---|---|
| target LUID 取得不可 | `deploy-context.md` 不整合 / 親プロジェクト削除済 | prep-extractor Phase B を再実行して `deploy-context.md` を作り直す |
| `create_project.py` が 403 | サービスアカウントの権限不足 | [authentication.md](authentication.md) のサービスアカウント設計を確認 |
| top-level 作成 WARNING に対するユーザー判断 | org governance | 承認/拒否はユーザー側で判断、Skill は強制しない |
