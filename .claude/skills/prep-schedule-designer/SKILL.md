---
name: prep-schedule-designer
description: 分解後 Prep フロー群 (int/mart) の定期実行スケジュールを設計し、Cloud UI で Linked Task を作成するための設計資料 (schedule-setup-runbook.md + schedule-design.json) を生成する Skill。run-type (Full/Incremental) は decomposed .tfl の実体スキャンで、依存順は LoadSqlProxy スキャンで機械確定し、facts-last の実行順・トリガ (曜日/時刻)・旧スケジュールの削除対象を 1 枚にまとめる。人間の UI セットアップ後は verify モードで設計とサーバー実測 (tasks/linked) を突合する。移行完了後に「スケジュールを設計して」「Linked Task の設計資料を作って」「定期実行を組みたい」「スケジュール設定を検証して」と言われたときに起動。スケジュールの API 作成・旧スケジュール削除はしない (Linked Task は UI 専用)。Cloud は読み取りのみ。
context: fork
agent: general-purpose
allowed-tools: Read Write Bash(python *) Glob Grep
---

# prep-schedule-designer

分解後フロー群のスケジュールを **設計** (design モード) し、人間が Cloud UI で Linked Task を作成した後に **検証** (verify モード) する Skill。**読み取り専用** (Cloud への書き込み副作用なし)。設計規範は [references/scheduling-model.md](references/scheduling-model.md)、出力形式は [references/runbook-format.md](references/runbook-format.md)。

Linked Task は REST で作成できない (UI 専用) ため、成果物は「人間がこの 1 枚だけで UI 再現できる設計資料」と「事後の機械突合レポート」。run-type と依存順は**設計文書から転記せず .tfl 実体から機械確定する** — これが本 Skill の存在理由 (文書 drift による run-type 誤記は append 出力の重複事故に直結する)。

## Caller から渡される入力

fork は会話履歴を渡せないため起動時に以下を文章で明示する ([fork-skill-contract.md §1](../../../references/fork-skill-contract.md)):

| 入力 | モード | 必須 | 例 |
|---|---|---|---|
| `mode` | — | ✅ | `design` または `verify` |
| `manifest_paths` | design | ✅ | 対象セッションの publish-manifest JSON パスの**明示列挙** (glob 探索はしない — 別セッション残骸の混入防止)。形式は [publish-manifest-format.md](../../../references/publish-manifest-format.md) |
| `output_dir` | 両方 | ✅ | 成果物出力先 (典型: `work/<yyyymmdd>_<tag>/reports/`) |
| `trigger_policy` | design | ✅ | ドメイン別トリガの合意事項を文章で (既定・曜日限定の規範は [scheduling-model.md §ドメイン分割とトリガ設計](references/scheduling-model.md))。元スケジュールの実測は Step 2 の probe が出す |
| `design_path` | verify | ✅ | design モードが出力した `schedule-design.json` のパス |
| `old_schedule_notes` | design | 任意 | 旧スケジュールの扱いに関する合意 (削除タイミング等) があれば |

## 出力

`output_dir` 配下:

- design モード: **schedule-setup-runbook.md** (人間向け) + **schedule-design.json** (verify 入力) + `schedule-inputs.json` / `schedule-probe.json` (中間、デバッグ用に残す)
- verify モード: **schedule-verify-report.md**

戻り値末尾の `## Timing` ブロックと verify モードの verdict 要約は [fork-skill-contract.md §2](../../../references/fork-skill-contract.md) に従う。

## ワークフロー — design モード

進捗:

- [ ] Step 1: collect (manifests + .tfl → schedule-inputs.json)
- [ ] Step 2: probe (サーバー現状 → schedule-probe.json)
- [ ] Step 3: ドメイン設計 (成分の束ね + トリガ確定)
- [ ] Step 4: runbook + design JSON 出力

### Step 1: collect

```bash
python ${CLAUDE_SKILL_DIR}/scripts/collect_schedule_inputs.py \
  --manifest <manifest_path_1> --manifest <manifest_path_2> ... \
  --out <output_dir>/schedule-inputs.json
```

manifest 群から int/mart フロー (kind=tfl) を集約し、各 .tfl から run-type (incremental/full + control field)・依存エッジ・facts-last の suggested_order・連結成分を機械確定する。stg (pds_augment) は自動除外。WARNING (inert incremental 設定・重複 flow 名) は runbook に転記する。

### Step 2: probe

```bash
python ${CLAUDE_SKILL_DIR}/scripts/probe_flow_schedules.py \
  --inputs <output_dir>/schedule-inputs.json --out <output_dir>/schedule-probe.json
```

既存 runFlow タスク・Linked Task (メンバー順含む)・**対象フローへのスケジュール衝突**・**stale look-alike フロー**を読み取る。認証失効の扱いは [fork-skill-contract.md §4](../../../references/fork-skill-contract.md)。

### Step 3: ドメイン設計

- schedule-inputs.json の **components (連結成分) を最小単位**とし、`trigger_policy` と probe の旧スケジュール実測 (どのフロー群が同一トリガだったか) を踏まえて 1 Linked Task = 1 ドメインに束ねる。束ね方・既定の規範は [scheduling-model.md §ドメイン分割とトリガ設計](references/scheduling-model.md)
- 実行順は成分内の `suggested_order` (facts-last) を連結する。**hub mart の位置と run-type は変更しない** (機械確定値が正)
- トリガ: `trigger_policy` に従い、曜日・JST/UTC を確定。probe で対象フローに既存スケジュールが見つかった場合は衝突として runbook 冒頭に警告する

### Step 4: 出力

[references/runbook-format.md](references/runbook-format.md) のテンプレートに従い runbook と design JSON を書く。両者の LUID / run-type / 順序は schedule-inputs.json からの転記で完全一致させる。stale look-alike が出たドメインには UI URL 列を付ける。旧スケジュール (Linked Task + standalone の両方) の削除対象一覧を末尾に載せる。

## ワークフロー — verify モード

進捗:

- [ ] Step 1: verify 実行
- [ ] Step 2: 結果要約を caller に返す

```bash
python ${CLAUDE_SKILL_DIR}/scripts/verify_schedules.py \
  --design <design_path> --out <output_dir>/schedule-verify-report.md
```

機械突合できるもの / できないもの (run-type は REST 不可視 → 挙動チェックリストに切替) は [references/scheduling-model.md §検証の限界](references/scheduling-model.md) 参照。レポートの overall_verdict / ドメイン別 verdict / cross-domain issues (未スケジュール・二重発火) / 旧スケジュールの現 state を caller に要約して返す。**fail の修正は行わない** — caller が人間の UI 再作業を案内するか、設計側の誤りなら design モードを再実行する。

## 失敗時の動作

失敗時は停止して caller にエラーを返す ([fork-skill-contract.md §3](../../../references/fork-skill-contract.md)、認証失効は §4)。本 Skill 固有のよくある失敗:

- manifest の tfl_path が解決できない: manifest と .tfl の相対配置が崩れている。caller にセッションフォルダ構成の確認を返す
- flow_luid が null: prep-deployer の publish が未完了。当該フローは LUID 無しで収集される (WARNING) が、design JSON には載せられない — publish 完了後に再実行
- 依存サイクル検出 (collect の WARNING): .tfl の参照が循環している。設計以前の問題なので caller に差し戻す

## 依存

Python: `tableauserverclient` (repo の requirements.txt に含まれる)。repo 共通の `scripts/flow_io.py` と、サーバー認証用の `scripts/tableau_auth.py` ([fork-skill-contract.md §4](../../../references/fork-skill-contract.md)) を import する。
