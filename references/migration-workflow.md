---
purpose: Prep フロー移行セッションの entry-point 手順書。main agent が各 Skill を呼ぶ順序・goal ゲート・停止点 (Stop 1/2) を規定する正典
note: 移行系の依頼 (分析 / 分解設計 / 移行 / publish / E2E 比較 / スケジュール / WB・Pulse repoint / backfill) を受けたら、他の作業の前に本ファイルを読み step 0 から実行する。フロー内設計は tableau-prep-architect の decomposition-plan、セッション横断の計画台帳は tableau-prep-migration-planner の migration-plan が正で、本ファイルは順序・intake・停止点のみを持つ
---

# Migration Workflow

## Step 0: Session intake

各 Skill は「必要な入力が会話に出ている」前提で動く。Skill を呼び始める前に、必要な入力を **1 ターンでまとめて** 聞く。

| # | 質問 | 必須条件 | 用途 |
|---|---|---|---|
| Q1 | 元フローの所在 — ローカル `.tfl/.tflx` パス、または Cloud の flow 名 / URL / LUID | 常に | step 1 の入力。サーバー DL は tableau-prep-extractor の `list_flows.py` / `download_flow.py` |
| Q2a | goal 深度 — ① 分析 / ② 分解設計 / ③ .tfl 生成 / ④ publish & run / ⑤ E2E 比較 | 常に | ④ 以上 = サーバー書込の包括合意 (以後は承認プロンプトなしの自律実行)。⑤ は元フローが Cloud 稼働中であること |
| Q2b | 横断工程 — schedule / repoint (WB / Pulse) / backfill の複数選択。不明なら空欄可 (step 3 の consumer probe が実測し Stop 1 で提案) | 任意 | tableau-prep-migration-planner の `--crosscut`。schedule はトリガ方針 (曜日限定ドメインの有無) の追加確認が要る |
| Q3 | 作業フォルダ名 — `work/<yyyymmdd>_<tag>/` の `<tag>`。空欄なら Q1 のフロー名から自動生成して復唱 | 常に | 全成果物の置き場 ([CLAUDE.md §work/ ディレクトリ規約](../CLAUDE.md#work-ディレクトリ規約)) |
| Q4 | target path — publish 先 project path (任意深さ) または LUID | goal ≥ ② | step 2 (`get_project_structure.py --project-path`) の入力 |
| Q5 | 既存 migration-plan — 前セッションの `work/<yyyymmdd>_<tag>/reports/migration-plan.json` パス | resume 時のみ | status を manifest 群と突合して再導出 |

- Q4 が自然言語なら path への変換は main agent の責務 (既存階層確認 → 復唱合意 → 確定)。URL / vizportalUrlId の解決は [tableau-prep-extractor の deploy-context-procedure.md](../.claude/skills/tableau-prep-extractor/references/deploy-context-procedure.md)
- `.env` の確認は遅延でよい: goal ≥ ③ または Q1 がサーバー DL のときのみ必要。step 2 直前に未整備なら聞く
- AI が値を決めた場合の復唱 (Q3 自動生成など) は省略しない
- resume (Q5 あり) は既存 plan の scope / 移行順 / target を追認し、Q1-Q4 は差分のみ確認する

## Workflow

step 0 合意後、Stop 1 / Stop 2 以外は承認を挟まず一気通貫で進む。失敗は [autonomous-recovery](../.claude/skills/tableau-prep-deployer/references/autonomous-recovery.md) で分類し、回復可能種別は自律リトライ、回復不能種別 (認証 / 権限 / 容量 / Cloud 障害 / loop 検知) は escalation。

| step | 担当 | 実行条件 | 成果物 / 動作 |
|---|---|---|---|
| 1 extract | tableau-prep-extractor Phase A | 常に | flow-summary.md + flow.json |
| 2 cloud context | tableau-prep-extractor Phase B | goal ≥ ② | deploy-context.md + input-dispatch-mech.json (Cloud 読み取りのみ) |
| 3 計画材料 | tableau-prep-extractor Phase C / consumer probe / tableau-prep-migration-planner init | Phase C は複数フロー時。probe は元フローが Cloud 稼働中の時。planner は複数フロー or Q2b あり時 | flow-dependencies.md (+ .json) / consumer 実測 / migration-plan.md + .json |
| — **Stop 1** (薄い) | 会話 | migration-plan を作った時のみ | scope / 移行順 / 横断工程 / 人間作業を提示。repoint 要否は probe 実測 (WB 数 / Pulse 定義数 / follower 数) を添えて提案。`OK` で先へ ([orchestration-model](../.claude/skills/tableau-prep-migration-planner/references/orchestration-model.md)) |
| 4 preflight | tableau-prep-deployer → tableau-prep-extractor Phase B 再実行 | goal ≥ ④ | layer プロジェクトを idempotent 作成 (サーバー書込) → deploy-context.md を更新して layer LUID を充填 |
| 5 analyze | tableau-prep-architect | 常に | analysis-`<flow>`.md |
| 6 decompose | tableau-prep-architect | goal ≥ ② | decomposition-plan-`<flow>`.json (設計の正) + .md / .html |
| — **Stop 2** (1 ターン) | 会話 | 常に | plan の Tier 1 を .html で視覚レビュー。`OK` で build へ ([review-checkpoints](../.claude/skills/tableau-prep-architect/references/review-checkpoints.md)) |
| 7 build | tableau-prep-builder | goal ≥ ③ | 新 .tfl 群 + augmenter spec、publish-manifest.json を init |
| 8 publish + run | tableau-prep-deployer | goal ≥ ④ | レイヤ順 (stg → int → marts) に publish → run → finishCode=0。同レイヤ内は並列可、レイヤ間は順次。完走後 resolve-luids |
| 9 compare | tableau-pds-comparator | goal ⑤ | 元 PDS vs 分解後 PDS の列差分 + 行数差分 Markdown |
| 10 schedule | tableau-prep-schedule-designer | Q2b で選択時 | runbook + design.json → Linked Task は人間が UI 作成 → verify モードで実測突合 |
| 11 repoint | tableau-workbook-repointer / tableau-pulse-repointer | Q2b で選択時 | design → repoint (承認ゲート付き) → verify。WB lineage に Pulse 消費は写らないため両方を別走査 |
| 12 backfill | tableau-pds-backfiller | compare 後にユーザーが明示要求した時のみ (既定の一気通貫に含めない) | 旧 output PDS 履歴の seed (段取りゲート付き) |

- consumer probe は main agent が実行する read-only CLI: `python scripts/consumer_probe.py --pds-name <旧output名> ...` ([scripts/README.md](../scripts/README.md))
- kind dispatch (tfl は publish+run / pds_augment は publish のみ)・needs_provisioning の build skip・incremental run 規律などの実装詳細は各 SKILL.md が持つ

## deploy-context ライフサイクルと goal ゲート

- preflight は作成した layer LUID を deploy-context.md に書き戻さない。step 4 の「preflight → Phase B 再実行」が正手順で、decompose の `gen_plan_skeleton` が更新後の deploy-context から layer LUID を plan.json に充填する。target が変わらない限り step 2 / 4 は 1 回でよい
- preflight のゲートは **goal ≥ ④** (Q4 の有無ではない)。goal ②/③ では plan.json の `flow_projects` / `ds_projects` は TODO placeholder のままでよい
- goal ③ の .tfl は Output projectLuid が placeholder のため publish 不可。機械ガード: `build_from_plan.py` は placeholder が残る plan を `--manifest` 指定時に fail させる。goal ③ の build では `--manifest` を付けない
- ③ → ④ 昇格: preflight → Phase B 再実行 → deploy-context から `flow_projects` / `ds_projects` を plan.json に転記 (設計フィールドは保持) → `build_from_plan.py` 再実行 → publish

## compare gap の targeted fix (フル再パスしない)

comparator 報告から影響 flow を特定 → tableau-prep-builder で plan.json の該当 entry を修正し `build_from_plan.py --only <flow名>` で部分再 build → tableau-prep-deployer で該当 flow とその下流のみ再 publish / 再 run。re-analyze / re-decompose / 全 flow 再 build は、gap の原因がレイヤ境界の設計自体にある場合のみ。

受け入れ判定は修正 flow 単体の finishCode=0 ではなく、その flow を含む下流連鎖 (Linked Task 単位 / レイヤ連鎖) の E2E 再実行で確認する — 入口の復旧で初めて下流の別 gap が露見することがある。

## 台帳の分担

| 台帳 | 範囲 | 書き手 |
|---|---|---|
| publish-manifest.json | セッション内の publish / run 状態と LUID ([publish-manifest-format.md](publish-manifest-format.md)) | tableau-prep-builder (init) + tableau-prep-deployer (update / resolve-luids)。読み手は tableau-pds-comparator |
| migration-plan.json | セッション横断の段取り (schedule / repoint / backfill 進捗)。正準置き場は初版セッションの `work/<yyyymmdd>_<tag>/reports/` | tableau-prep-migration-planner (init) + main agent (update / resume) |

publish 先構造のモデル (target = stg/int/marts の直上) は [project-hierarchy.md](project-hierarchy.md)。

## セッション運用 (速度・トークン)

- 複数フローはバッチをデフォルトにする: Phase C で依存を把握し、1 セッションに複数フローを載せて deploy-context / 設計パターンを再利用する。同一 target なら step 2 / 3 は 1 回で足りる
- 長大セッションの resume を避け、バッチごとに新セッションを開始する。文脈は `work/` 成果物と Q5 の migration-plan.json で引き継ぐ
