---
name: prep-pulse-repointer
description: 移行後、旧 Published Data Source を参照する Tableau Pulse の Metric Definition を新 marts PDS へ差し替える Skill。design (Pulse REST の definitions 全ページ走査 + subscriptions 棚卸しと publish-manifest の突合で対象定義・旧→新 PDS 対応・follower を機械確定し pulse-repoint-runbook.md + pulse-repoint-design.json を生成) / repoint (in-place の datasource 変更は API 不可のため、新 PDS 参照のコピー定義を作成し metric と follower 購読を再作成する自動差し替え。rehearsal コピー → insight 生成の証拠比較 → ユーザー承認 → production の段取りゲート必須) / verify (差し替え後にサーバー実測と突合) の 3 モード。「Pulse の参照置換」「Pulse 定義を新 PDS に差し替え」「Pulse repoint の設計資料を作って」「Pulse repoint を実行して」「Pulse の repoint を検証して」と言われたときに起動。旧定義の削除はしない (連鎖削除があるため人間判断)。サーバー書込は repoint モードの定義/metric/購読作成のみ (design / verify は読み取りのみ)。移行セッション冒頭の intake・goal ゲート・起動順序は references/migration-workflow.md が正典（本 Skill 単体で移行セッションを始めない）。
context: fork
agent: general-purpose
allowed-tools: Read Write Bash(python *) Glob Grep
---

# prep-pulse-repointer

移行後、旧 PDS を参照する Tableau Pulse の Metric Definition を新 marts PDS へ差し替える Skill。
**設計** (design)・**自動差し替え** (repoint)・**検証** (verify) の 3 モードを持つ。
[prep-workbook-repointer](../prep-workbook-repointer/SKILL.md) の Pulse 版 — WB lineage
(`downstreamWorkbooks`) には Pulse 消費が写らないため、別走査が必須。API の機構・制約は
[references/pulse-api-recipe.md](references/pulse-api-recipe.md)、出力形式は
[references/pulse-repoint-format.md](references/pulse-repoint-format.md)。

存在理由: 「どの Pulse 定義が・どの旧 PDS を参照し・どの新 PDS と follower 移行が要るか」を
Pulse REST と publish-manifest の突合で **機械確定** し、rehearsal の証拠付きで安全に差し替えること。
**Pulse は in-place の datasource 差し替え (PATCH) を許さない** (実測 404) ため、repoint は
「新 PDS 参照のコピー定義を作成 → metric 再作成 → follower 再購読」で実現する
([pulse-api-recipe.md](references/pulse-api-recipe.md) の確定レシピ)。

**サーバー書込は repoint モードのみ** (定義・metric・購読の作成、rehearsal コピーの削除)。
**旧定義の DELETE は本 Skill は実行しない** — 配下 metrics + subscriptions が連鎖削除されるため、
runbook に手順を記して人間判断に委ねる。

## Caller から渡される入力

fork は会話履歴を渡せないため起動時に以下を文章で明示する ([fork-skill-contract.md §1](../../../references/fork-skill-contract.md)):

| 入力 | モード | 必須 | 説明 |
|---|---|---|---|
| `mode` | — | ✅ | `design` / `repoint` / `verify` |
| `manifest_paths` | design | ✅ | 全移行セッションの publish-manifest JSON パスの**明示列挙** (glob しない — 別セッション残骸の混入防止)。形式は [publish-manifest-format.md](../../../references/publish-manifest-format.md)。`resolve-luids` 完了済みが前提 (旧/新 PDS の LUID が必要) |
| `source_project` | design | ✅ | 旧 PDS の所在プロジェクト名 (サイト固有。既定なし — caller が明示する)。この配下の PDS を参照する定義だけが repoint 対象になる |
| `output_dir` | 全モード | ✅ | 成果物出力先 (典型: `work/<yyyymmdd>_<tag>/reports/`) |
| `design_path` | repoint / verify | ✅ | design モードが出力した `pulse-repoint-design.json` のパス |
| `stage` | repoint | ✅ | `rehearsal` または `production`。**production はユーザーが rehearsal の証拠を見て明示承認した後にのみ渡される** (caller が保証する) |
| `target_definitions` | repoint | ✅ | 差し替える定義 ID の列挙、または「全件」の明示 |

## 出力

`output_dir` 配下:

- design モード: **pulse-repoint-runbook.md** (人間向け) + **pulse-repoint-design.json** (repoint / verify 入力) + `pulse-repoint-inventory.json` (中間、デバッグ用に残す)
- repoint モード: `RESULT_JSON` の per-definition 結果 (rehearsal: コピー定義 ID と新旧 insight 比較 / production: 新定義 ID・metric/購読の移行数・insight 検証)
- verify モード: **pulse-repoint-verify-report.md**

戻り値末尾の `## Timing` ブロックと verify モードの verdict 要約は
[fork-skill-contract.md §2](../../../references/fork-skill-contract.md) に従う。各スクリプトの
`RESULT_JSON:` 行に `elapsed_s` があるので集約する。

## ワークフロー — design モード

進捗:

- [ ] Step 1: inventory (Pulse REST → 左辺 = 定義 × 旧 PDS × follower)
- [ ] Step 2: build plan (manifest join → 右辺 = 旧 PDS → 新 PDS)
- [ ] Step 3: 戻り値要約

### Step 1: inventory

```bash
python ${CLAUDE_SKILL_DIR}/scripts/inventory_pulse.py \
  --source-project <source_project> \
  --out <output_dir>/pulse-repoint-inventory.json
```

`GET /api/-/pulse/definitions` を **`page_size=100` + `next_page_token` で全ページ走査**し
(既定 10 件で silent truncation する — [pulse-api-recipe.md](references/pulse-api-recipe.md))、
各定義の `specification.datasource.id` を REST `datasources.get` で名前解決して
`source_project` 配下参照の定義を対象に採る。site 全体の `subscriptions` も列挙し、
定義配下の metric ごとの follower を突合する。**利用状況フィルタはしない** (取捨は人間判断)。
認証失効の扱いは [fork-skill-contract.md §4](../../../references/fork-skill-contract.md)。

### Step 2: build plan

```bash
python ${CLAUDE_SKILL_DIR}/scripts/build_pulse_repoint_plan.py \
  --inventory <output_dir>/pulse-repoint-inventory.json \
  --manifest <manifest_path_1> --manifest <manifest_path_2> ... \
  --out-dir <output_dir>
```

ローカル join のみ (サーバーアクセスなし)。manifest との join で旧→新 PDS を機械確定し、
**pulse-repoint-design.json** と **pulse-repoint-runbook.md** を 1 パスで生成する (両者は必ず一致)。
join モデル (luid 主キー / name fallback / `unmapped_old_pds`) の正典は
[publish-manifest-format.md §repoint join model](../../../references/publish-manifest-format.md)
(prep-workbook-repointer と共通)。

runbook は **go/no-go 判断書** — ユーザーがこの 1 枚で影響全量を確認して repoint を承認できる
ことが目的。impact 表は **follower 有無で 2 階層** に分ける: follower あり = 移行対象 /
follower なし = **破棄候補** (未使用とみなし既定では repoint せず、カットオーバー時に旧定義ごと
削除。ユーザーは定義 id を repoint モードに渡すだけで昇格できる) — 定義が大量にあるサイトで
全件を移行判断させないための階層化。ほかに定義 id の変化・カットオーバーで消える範囲・
引き継がれないもの (id / insight 履歴)・残余リスクの注意喚起 (旧 PDS 直接利用は列挙不能 /
新 PDS 権限はユーザー責務)・カットオーバー前チェックリストを必ず含む
([pulse-repoint-format.md](references/pulse-repoint-format.md))。参照フィールド名も抽出して載せる —
フィールド不整合は **insight 生成時にしか顕在化しない** ため人間レビューの材料にする。

### Step 3: 戻り値要約

runbook のパス・対象定義数・follower 数・`unmapped_old_pds` 件数・warnings を要約して返す。
次は repoint モード (rehearsal から) に進む旨を添える。

## ワークフロー — repoint モード

**rehearsal と production は別 invocation** — fork 内ではユーザーと対話できないため、承認ゲートは
invocation の間 (メイン会話) に置く。caller は rehearsal の証拠をユーザーに提示し、明示承認を得て
から production で再起動する。

### stage = rehearsal

進捗:

- [ ] Step 1: rehearsal コピー作成 + insight 証拠取得
- [ ] Step 2: 証拠要約を caller に返す

```bash
python ${CLAUDE_SKILL_DIR}/scripts/repoint_pulse_definition.py \
  --design <design_path> \
  --definition <def_id> [--definition <def_id> ...] \
  --stage rehearsal
```

対象定義ごとに `<元名> (repoint rehearsal)` を新 PDS 参照で作成し (元定義は無傷)、**元定義と
コピーの両方で insight (BAN) を生成して値を比較**する。insight が両方 201 で値が一致すれば
新 PDS への差し替えは機能的に安全 (フィールド整合も同時に検証される)。戻り値には per-definition の
`markup` 比較と verdict (`match` / `differs` / `probe_failed`) を含め、**`differs` の解釈**
(新旧 PDS の freshness 差なら想定内、桁違いや probe_failed ならフィールド不整合か backfill 未了) を
caller が判断できる材料を添える。

### stage = production

進捗:

- [ ] Step 1: 本番差し替え (rename → create → metric/購読移行 → insight 検証)
- [ ] Step 2: 結果要約を caller に返す

```bash
python ${CLAUDE_SKILL_DIR}/scripts/repoint_pulse_definition.py \
  --design <design_path> \
  --definition <def_id> [--definition <def_id> ...] \
  --stage production
```

対象定義ごとに次の順で実行する (順序不変則: **follower 移行と insight 検証が終わるまで旧定義に
触れるのは rename だけ**):

1. 旧定義を `<元名> (pre-repoint)` に rename (PATCH — name 変更は可能)
2. rehearsal コピーを元の名前に rename して**昇格**し新定義とする (Pulse は同一
   datasource + specification の定義を 2 つ作れない — 409。rehearsal 未実施の場合のみ新規作成。
   specification は viz_state ごとコピーで `datasource.id` のみ差し替え — sqlproxy ラベルは
   不活性なので書き換えない)
3. **旧定義のライブ状態** (design スナップショットではない) から metric と subscription を
   読み直し、non-default metric を `metrics:getOrCreate` で再作成、follower を対応する新 metric に
   再購読する — design 後に増えた後発 follower の移行漏れを防ぐ
4. 新定義で insight を生成して機能検証 (失敗したら以降を中断し caller に返す —
   旧定義は rename されただけで生きているので巻き戻し可能)
5. 昇格でなく新規作成だった場合のみ、残った rehearsal コピーを削除 (**follower ゼロ確認 +
   昇格済み id を消さないガード付き**)

旧定義 (`(pre-repoint)`) の削除は本 Skill はしない。runbook 記載の手順で人間が
UI / API から削除する (削除で配下 metrics + subscriptions が連鎖削除されることを runbook に明記)。

## ワークフロー — verify モード

進捗:

- [ ] Step 1: 再走査で反映突合
- [ ] Step 2: 結果要約を caller に返す

```bash
python ${CLAUDE_SKILL_DIR}/scripts/verify_pulse_repoint.py \
  --design <design_path> --out <output_dir>/pulse-repoint-verify-report.md
```

design と同じ全ページ走査を 1 回実行し、design.json の**移行対象** (migration_scope=followed、
またはユーザーが昇格して repoint 済みの破棄候補) について「元の名前の定義が新 PDS を参照している」
「新定義の follower 数 ≥ 旧定義 (`(pre-repoint)`) の**ライブ**購読数」(旧定義が削除済みなら
この突合はスキップ)「新定義の insight が 201 を返す」で判定する。未昇格の**破棄候補は判定対象外**
だが、旧定義のライブ購読を監視し **後発 follower が現れたら警告** する (未使用前提の崩れ)。
旧定義の残存 (`(pre-repoint)` / rehearsal コピー) は warning として列挙する (削除は人間判断)。

戻り値には `overall_verdict` (`PASS` / `INCOMPLETE` / `EMPTY`) と要対応の定義一覧を含める。

## 失敗時の動作

失敗時は停止して caller にエラーを返す ([fork-skill-contract.md §3](../../../references/fork-skill-contract.md)、認証失効は §4)。本 Skill 固有のよくある失敗:

- 一覧が想定より少ない: `page_size` / `next_page_token` の追従漏れはスクリプト側で対処済みだが、
  総数が `total_available` と食い違う場合は caller に生数値を返して確認する
- `POST /definitions` が 404: 参照先 datasource が Pulse から解決できない (LUID 誤り / 権限不足 /
  削除済み)。design の新 PDS LUID を manifest と突合し直す
- insight probe が 400: 新 PDS に定義参照フィールドが無い (rename-back 漏れ等)。**定義側は直さず**
  caller に返す — mart 列名の修正 (prep-builder / prep-pds-augmenter) が正道
- `metrics:getOrCreate` / `subscriptions` 作成の失敗: 中断して caller に返す。部分移行の状態
  (どこまで作成済みか) を RESULT_JSON で明示する — 再実行は getOrCreate / 既存購読スキップで冪等
- 旧/新 PDS の LUID が null: manifest の `resolve-luids` が未完了。`python scripts/publish_manifest.py
  resolve-luids --manifest ...` を先に実行するよう caller に返す

## 依存

Python: 標準ライブラリのみ (Pulse REST は versionless path を直接叩く)。サーバーを触るスクリプトは
認証に repo 共通の `scripts/tableau_auth.py`、Pulse クライアントに repo 共通の
`scripts/pulse_api.py` を import する (consumer_probe.py と共用のため repo 直下に昇格済み。
[fork-skill-contract.md §4](../../../references/fork-skill-contract.md))。
build_pulse_repoint_plan.py はローカル完結でサーバー不要。Pulse は Tableau Cloud 専用
(Tableau Server では動かない — [pulse-api-recipe.md](references/pulse-api-recipe.md))。
