---
purpose: prep-pds-backfiller のライフサイクル (0-9) の詳細手順。各ステップの目的・スクリプト・ゲート・委譲先を規定する
note: SKILL.md の checklist の実行本体。スクリプトは skill の scripts/ にあり、cross-skill の run / 再 run は prep-deployer に委譲する
---

# backfill 実行 recipe

incremental フロー移行後、旧 output PDS の履歴を新 accumulator PDS へ一度だけ seed するライフサイクル。**取り消しにくい書き込み (本番 Overwrite) を含むため、段取りそのものが安全要件**。dry-run → sandbox preview → 明示承認 → 本番、の順を崩さない。

## 目次

- 全体像
- Step 0: 前提条件チェック
- Step 1: schedule interlock
- Step 2: 対象・列・seam の確定 (ユーザーゲート①)
- Step 3: dry-run
- Step 4: sandbox preview
- Step 5: 本番 Overwrite (ユーザーゲート②)
- Step 6: 事後検証 + 受け入れ incremental
- Step 7: 下流再 run
- Step 8: schedule 再開
- ロールバック

## 全体像

```
0 前提条件チェック ──(refuse 条件に該当なら中止)
1 schedule interlock ──(Active schedule は UI で suspend, in-flight run 無しを確認)
2 対象/列/seam 確定 ★ゲート① seam か replace か
3 dry-run          ──(ローカル挿入 + 行数検算, 本番未書込)
4 sandbox preview  ──(使い捨て project へ別名 publish, GUI 確認)
5 本番 Overwrite   ★ゲート② 明示承認 (snapshot は自動退避済み)
6 事後検証 + 受け入れ incremental 1 サイクル
7 下流再 run       ──(prep-deployer に委譲)
8 schedule 再開    ──(UI)
失敗時: --restore <tag> で snapshot から即時ロールバック
```

## Step 0: 前提条件チェック

[preconditions-and-edge-cases.md](preconditions-and-edge-cases.md) の refuse 条件に該当しないか判定する。**いずれか該当なら escalate して中止**:

- 対象が実は full-refresh 出力 (誤ラベル) → 次回 run で seed が消える
- 旧 PDS が既に削除・不在 → 履歴の供給源が無い
- スキーマが rename / cast で吸収不能 → 挿入不能
- 下流が cross-day 履歴に依存し gap 中に劣化する (DoD・window LOOKUP・移動平均等)
- int 層を持たない single-table passthrough で層設計が未決 → 対象 PDS 名が変わりうる (別タスク決着まで保留)

## Step 1: schedule interlock

accumulator の定期 run と本番 Overwrite の競合を締め出す。**判定は時刻ベース** — active schedule の存在だけで止めない。

```bash
python check_flow_readiness.py --flow-luid <accumulator-flow-luid>   # --window-minutes 60 (既定)
```

- `ready_for_overwrite: true` なら進む。判定内訳:
  - **imminent な active schedule** (次回 run が window 内 = 操作中に発火しうる) → **BLOCKER**。この場合のみ **Cloud UI で当該 schedule を Suspend** する (Linked Task は REST で suspend できない → 手動ゲート)、suspend 後に再確認
  - **distant な active schedule** (次回 run が window 外) → **ADVISORY (blocker ではない)**。suspend は不要。backfill + 受け入れ run を次回 run より前に終える + schedule の run-type が Incremental であることを確認 (seam は watermark 保存なので Incremental run は二重化しない。Full run だけが二重化するが、それは backfill と無関係な既存のスケジュール不備)
  - `running_flow_jobs` に name-matched の in-flight run → **BLOCKER**、完了を待つ
- この interlock は Step 5 の本番 Overwrite 直前にも再確認する (dry-run/preview の間に in-flight run が挟まっていないか、window が縮んでいないか)

## Step 2: 対象・列・seam の確定 (ユーザーゲート①)

**対象 accumulator を deployed flow から解決する** (計画書・manifest の LUID はドリフトする):

```bash
python resolve_accumulator.py --flow-luid <flow-luid> --out accumulators.json
```

`classification: accumulator` の出力ノードだけが backfill 対象。`full_refresh` / `inert_incr` は対象外。`pds_luid` が解決できていればそれを spec の `new_luid` に使う。

**列整合を検証する** (旧・新のメタデータ JSON を `mcp__tableau__get-datasource-metadata` で取得済みの前提):

```bash
python diff_pds_schema.py old.metadata.json new.metadata.json --control <control> --json-out diff.json
```

`CLEAN` なら `column_map` 不要。`only_old` / `only_new` / `type_mismatch` が出たら、rename は `--rename OLD=NEW` で吸収を試し、残る差は spec の `column_map` (rename / cast) にする。吸収不能なら Step 0 の refuse に戻る。

**★ゲート①: seam か replace か をユーザーに選ばせる**。baseline の中身 (新の行数・control の MIN/MAX/distinct) を提示して判断材料にする。判断基準は [preconditions-and-edge-cases.md](preconditions-and-edge-cases.md) の sentinel 節。自動判定に委ねない。

確定した内容を spec JSON にする ([backfill-spec-format.md](backfill-spec-format.md))。

## Step 3: dry-run

ローカルで backfill 抽出を組み、行数を検算する。**本番未書込**。

```bash
python backfill_pds.py --spec backfill-spec.json --only <tag>
```

- 新 .tdsx は download 時に `snapshot/` へ自動退避される (Step 5 のロールバック点)
- `local_verify: OK` (実測 == `expected_new_total`) を確認。`MISMATCH` なら中止して原因調査
- `sentinel_warning: true` が出たら seam モードが 0 挿入している → ゲート① に戻って replace を検討
- 出力: `<tag>_backfilled.tdsx`、`report_dryrun.json`、`backfill-manifest.json`

## Step 4: sandbox preview

seed 済み抽出を使い捨て project へ別名 publish し、GUI で実データを目視する。**非破壊** (本番 PDS 不変)。

```bash
python publish_preview.py --spec backfill-spec.json --workdir <workdir> \
  --parent-path <sandbox-path> --project <throwaway-child> --only <tag>
```

preview PDS は `<本番名>__backfill_preview` として publish される。Tableau で開いて control 範囲・行数・値を確認する。

## Step 5: 本番 Overwrite (ユーザーゲート②)

**★ゲート②: 取り消しにくい書き込み。ユーザーの明示承認を得てから実行する**。Step 1 の interlock を再確認 (schedule suspend 済み・in-flight run 無し)。

```bash
python backfill_pds.py --spec backfill-spec.json --only <tag> --execute
```

- 事前に `local_verify` が OK でなければ publish を拒否する (スクリプトが RuntimeError)
- publish は Overwrite (LUID / content_url 不変)。manifest に `executed: true` / `published_luid` / `server_verify` を記録

## Step 6: 事後検証 + 受け入れ incremental

`--execute` はサーバー再検証を内蔵する (`server_verify`): 再 DL して行数 == 期待値、MAX(control) が seam モードで不変 (replace は old_max) を確認。`verdict: MISMATCH` なら即ロールバック (末尾参照)。

続けて **受け入れ incremental を 1 サイクル**実走し、二重化ゼロ・正しい追記を確認する (継続性の実証)。run は prep-deployer に委譲 (repo 直下からの相対パス):

```bash
python .claude/skills/prep-deployer/scripts/run_flow.py --flow-id <accumulator-flow-luid> --incremental
```

run 前後で accumulator の行数を比較し、**seam 超の新規ソース行だけが増えた** (過去区間は不変) ことを確認する。全区間が二重化していたら full で走った兆候 → UI で run-type を Incremental に直す。

## Step 7: 下流再 run (Mart へ反映)

accumulator (int) に履歴を入れても、その上の **full-refresh mart / cross-flow consumer は再 run しないと反映されない**。**WB 置換も E2E parity も Mart PDS で consume する**ので、この工程まで終えて初めて backfill が実運用に効く。

- **下流 Mart は必ず FULL run** (`run_flow.py --flow-id <mart>`、`--incremental` を付けない)。Mart は full-refresh (replace) なので full で二重化せず、backfill 済み int を丸ごと読んで作り直す。**int は incremental 限定 / Mart は full 限定**の非対称を守る (逆にすると int は二重化・Mart は無意味)
- **依存順に発火**: int (backfill 済) → 直上 Mart (full) → mart-on-mart consumer (full)。順序は session `publish-manifest.json` と LoadSqlProxy lineage、または移行計画のドメインチェーンから辿る
- 発火前に対象が本当に full-refresh か `resolve_accumulator.py` で確認する (誤って append なら full run で二重化する)
- 効果の検証: Mart 行数が int と整合し、履歴依存の派生列 (DoD / window LOOKUP / 移動平均) が null から回復していること (最古期間の行のみ前区間不在で null が残るのは正しい)
- run 自体は prep-deployer の `run_flow.py` に委譲 (書き込み責務の分離)。本 Skill は下流の特定と run-type (FULL) の指定まで

## Step 8: schedule 再開

Step 1 で suspend した schedule を Cloud UI で再開する。受け入れ (Step 6) が OK になってから再開する。

## ロールバック

本番 Overwrite が想定外なら snapshot から即時復旧する:

```bash
python backfill_pds.py --spec backfill-spec.json --restore <tag>
```

manifest の `snapshot_tdsx` (seed 前の新 accumulator) を同一 LUID へ Overwrite し直す。旧 PDS も削除していないので、最悪でも accumulator を full で再 baseline すれば復旧できる (backfill 分は再消失するため snapshot 復旧が一次手段)。
