---
name: prep-migrate
description: Tableau Prep フロー移行セッションの entry-point 手順書。Session intake (Q1-Q5)・workflow (extract → analyze → decompose → build → publish → run → compare → schedule → repoint)・Stop 1/2 の運用・deploy-context ライフサイクル (preflight → Phase B 再実行) と goal ゲート・失敗時の targeted fix ループを規定し、main agent が各 Skill を正しい順序と goal 段階ゲートで呼び出すための正典。ユーザーが Prep フローの分析 / 分解設計 / 移行 / Cloud publish / E2E 比較 / スケジュール設計 / Workbook repoint / Pulse repoint / backfill を依頼したら、他の作業に入る前にセッション冒頭で必ず起動する。フロー内設計は prep-architect、セッション横断の計画台帳は prep-migration-planner が担い、本 Skill はそれらを呼ぶ順序と intake・停止点のみを持つ。
---

# prep-migrate

Prep フロー移行セッションの **entry-point 手順書**。main agent が各 Skill を「どの順序で・どの goal ゲートで・どこで停止して」呼ぶかを規定する。ユーザーが Prep フローの分析・分解設計・移行・Cloud publish・E2E 比較・スケジュール設計・Workbook repoint・Pulse repoint・backfill を依頼したら、他の作業に入る前にセッション冒頭で起動する。

本 Skill は `context: fork` を **付けない** — intake の質問・Stop 1/2 のユーザー確認・失敗観測・下流 Skill への決定の受け渡しを**主会話コンテキストで**扱うため。生成物は持たず (各 Skill が成果物を作る)、workflow・intake・停止点の規範だけを提供する Reference Contents。フロー内設計 (命名 / レイヤ / Input policy) は [prep-architect](../prep-architect/SKILL.md) の `decomposition-plan-<flow>.json` が正、セッション横断の計画台帳は [prep-migration-planner](../prep-migration-planner/SKILL.md) の `migration-plan` が正で、本 Skill はそれらへの踏み込みをしない。

## Workflow

ユーザーが既存 Prep フローを指して「分析して」「分解設計して」「dbt 風に整理して」「Tableau Cloud に publish して」「実行して」「E2E 比較して」と指示したら、各 Skill を **順次または個別に** 実行する。**Session intake (step 0) で goal / target path を確定したら、extract → (goal ≥ ④ なら preflight → Phase B 再実行) → analyze → decompose まで段階間の承認を取らず一気通貫** (複数フロー or 横断工程を含む移行では step 0b で migration-plan を骨生成し、薄い Stop 1 を 1 回挟む)。**decompose 完了後に Stop 2 ユーザー確認 (1 ターン) を 1 回だけ取り、`OK` で build → publish → run → compare まで再び一気通貫**。失敗時は AI が原因を機械判定し、回復可能な種別は自律ループでリトライ、回復不能な種別 (認証 / 権限 / 容量 / Cloud 障害 / loop 検知発火) は escalation ([autonomous-recovery](../prep-deployer/references/autonomous-recovery.md))。

```
[step 0]   Session intake (会話)       Q1-Q5 を 1 ターンで聞く (§Session intake)
[Phase A]  prep-extractor              .tfl/.tflx → flow-summary.md + flow.json (構造抽出)
[step 0a]  prep-extractor Phase B      (goal ≥ ②) target_path walk + Input kind 分類 + PDS LUID 解決
                                       → deploy-context.md 初版 (layer 未作成なら presence=no) + input-dispatch-mech.json
                                       (Cloud 読み取りのみ)
[step 0b]  prep-extractor Phase C      (複数フロー時) 依存抽出 → flow-dependencies.md (+ --json)
           consumer probe              (goal ≥ ② かつ元フローが Cloud 稼働中) 旧 output PDS ごとの
                                       consumer 数を read-only で実測 → repoint 要否の推奨
                                       `python scripts/consumer_probe.py --pds-name <旧output名> ...`
                                       (main agent が実行。WB lineage に Pulse 消費は写らないため
                                        WB / Pulse の両方を数える)
           prep-migration-planner      (複数フロー or 横断工程時) 移行計画書の骨生成 → migration-plan.md + .json
★ Stop 1 (薄い) ユーザー確認 ★         scope / 移行順 / 横断工程適用 / 人間作業段取りを提示、OK で先へ
                                       repoint 要否は probe の実測を添えて提案する (「WB N 件 /
                                       Pulse M 定義 (follower 付き K) が接続中 → repoint 推奨」/
                                       「未 follow の Pulse 定義のみ → 破棄整理のみ」/「consumer なし」)
                                       ([orchestration-model](../prep-migration-planner/references/orchestration-model.md))
[step 0c]  prep-deployer preflight     (goal ≥ ④ のみ) pending segments + flows/・datasources/ × dbt 3 レイヤを
                                       idempotent 作成 (サーバー書込)
[step 0c'] prep-extractor Phase B 再実行 (goal ≥ ④ のみ) deploy-context.md を更新 — 作成済み layer LUID が埋まる
           prep-architect analyze      現状把握 → analysis-<flow>.md
           prep-architect decompose    分解設計 → decomposition-plan-<flow>.json (設計の正) + .md/.html (レビュー用)
                                       gen_plan_skeleton が deploy-context の layer LUID を plan.json に充填
                                       (goal ②/③ は preflight/0c' 未実行 → flow_projects/ds_projects は TODO placeholder)
★ Stop 2 ユーザー確認 (1 ターン) ★     plan の Tier 1 を明示確認 (.html をブラウザで開いて視覚レビュー)、OK で build へ
                                       ([review-checkpoints](../prep-architect/references/review-checkpoints.md))
[Phase 3]  prep-builder build          plan → 新 .tfl 群 + augmenter spec、publish-manifest.json を init
           prep-deployer publish+run   レイヤ単位 (stg → int → marts) に publish → run → finishCode=0 確認。
                                       同レイヤ内は並列可、レイヤ間は順次。manifest update、完走後 resolve-luids
           prep-output-comparator      元 PDS vs 分解後 PDS の列差分 + 全体行数差分 → Markdown
           prep-schedule-designer      スケジュール設計 → schedule-setup-runbook.md + schedule-design.json
                                       (Linked Task は UI 専用 → 人間が UI 作成) → verify モードで実測突合
[Phase 4]  prep-workbook-repointer     design: 旧 PDS 参照 WB の棚卸し + 旧→新 PDS 対応
                                       → repoint-runbook.md + repoint-design.json
                                       → repoint モード (既定): rehearsal 手術 publish → 証拠レポートを
                                         ユーザーに提示 → 明示承認 → production Overwrite (サーバー書込)
                                         (手術不可ケース・権限制約時のみ fallback = 人間が runbook を
                                         見て Desktop の Replace Data Source で差し替え + republish)
                                       → verify モードで lineage 反映を突合 (読み取りのみ)
           prep-pulse-repointer        design: 旧 PDS 参照 Pulse 定義 + follower の棚卸し + 旧→新 対応
                                       → pulse-repoint-runbook.md + pulse-repoint-design.json
                                       → repoint モード: rehearsal コピー + insight 比較の証拠を
                                         ユーザーに提示 → 明示承認 → production (コピー定義作成 +
                                         metric/購読再作成、サーバー書込。旧定義削除は人間判断)
                                       → verify モードで実測突合 (読み取りのみ)
                                       WB lineage に Pulse 消費は写らないため WB repoint とは別走査
[任意]     prep-pds-backfiller         incremental accumulator に旧 output PDS の履歴を seed。移行完了後の
                                       別工程で、ユーザーが「backfill して」と言ったときのみ。段取りゲート付き
                                       (dry-run → sandbox preview → 明示承認 → 本番 Overwrite → 受け入れ incremental)
```

kind dispatch (kind=tfl は publish+run / kind=pds_augment は publish のみ)・needs_provisioning の build skip・incremental run 規律などの実装詳細は各 SKILL.md と recipe が持つ (この図には書かない)。**backfill は既定の一気通貫には含めない** — 履歴 seed の要否・seam/replace・本番承認がフロー単位のユーザー判断なので、compare 後にユーザーが明示要求したときだけ prep-pds-backfiller を起動する。

### deploy-context ライフサイクルと goal ゲート

- **preflight の書き戻しはしない**: preflight スクリプトは作成した layer LUID を deploy-context.md に書き戻さない。正手順は `0c preflight → 0c' Phase B 再実行` で deploy-context.md を更新し、decompose の `gen_plan_skeleton` がそこから layer LUID を plan.json に充填する。同一 target で analyze / decompose / build を反復するときは **0c' 後の deploy-context.md を再利用**する (target が変わらない限り preflight / Phase B 再実行は 1 回でよい)。
- **preflight は goal ≥ ④ ゲート**: preflight (プロジェクト作成 = サーバー書込) は goal ≥ ④ のときだけ実行する。ゲート基準は Q4 target path の有無ではなく **goal 段階**。goal ②/③ では preflight も Phase B 再実行もせず、plan.json の `flow_projects` / `ds_projects` は `gen_plan_skeleton` の TODO placeholder のまま許容する。**goal ③ で生成した .tfl は Output projectLuid が placeholder のため publish 不可** (機械ガード: `build_from_plan.py` は placeholder が残る plan を `--manifest` 指定時に fail させ、無指定時は WARNING でローカル build のみ許容する)。goal ③ の build では `--manifest` を付けない。
- **③ → ④ 昇格手順**: preflight → Phase B 再実行 → 更新後の deploy-context から `flow_projects` / `ds_projects` を plan.json に転記 (設計フィールドは保持) → `build_from_plan.py` 再実行 → publish。

### compare で gap が出たら targeted fix で直す (フル再パスしない)

メインエージェントが comparator 報告から影響 flow を特定 → prep-builder に plan.json の該当 entry 修正 + `build_from_plan.py --only <flow名>` での部分再 build を指示 → prep-deployer で該当 flow とその下流のみ再 publish / 再 run。re-analyze / re-decompose / 全 flow 再 build は、gap の原因がレイヤ境界の設計自体にある場合のみ。

**fix の受け入れは修正 flow 単体の finishCode=0 ではなく、その flow を含む下流連鎖 (Linked Task 単位 / レイヤ連鎖) の E2E 再実行で確認する** — 入口の復旧で初めて下流の別 gap が露見することがある。

### session manifest と migration-plan の分担

session manifest (`publish-manifest.json`) は 1 セッションの **元フロー LUID / 元 output PDS LUID / 分解後フローの publish & run 状態 / 分解後 output PDS LUID** をまとめた単一 JSON。形式は [references/publish-manifest-format.md](../../../references/publish-manifest-format.md)、書き込みは prep-builder (init) + prep-deployer (update / resolve-luids)、読み取りは prep-output-comparator。**セッションを跨ぐオーケストレーション状態 (schedule / repoint / backfill の進捗) は `migration-plan.json` (prep-migration-planner) が持つ** — publish-manifest がセッション内の publish/run 状態、migration-plan がセッション横断の段取り台帳、と役割が分かれる (二重管理ではない)。migration-plan.json の**正準置き場は初版を生成したセッションの `work/<yyyymmdd>_<tag>/reports/migration-plan.json`**。以後のセッションは intake (Q5) でそのパスを受け取り、status を manifest 群と突合して再導出する。

publish 先構造のモデル (target = stg/int/marts の直上、上位は任意の深さ・命名) は [references/project-hierarchy.md](../../../references/project-hierarchy.md)。

## Session intake (step 0)

各 Skill は「必要な入力が会話に既に出ている」前提で動く。メインエージェントが Skill を呼び始める前に、必要な入力を **1 ターンでまとめてユーザーに聞いておく** (遅延収集は確認往復が増えるので避ける)。

セッション冒頭で聞く項目:

| # | 質問 | 必須条件 | 受け取り後の使い道 |
|---|---|---|---|
| **Q1. 元フローの所在** | ローカル `.tfl/.tflx` パス、または Tableau Cloud 上の flow 名 / URL / LUID | 常に必須 | Phase A 入力。サーバー DL は prep-extractor の `list_flows.py` / `download_flow.py` 経由 |
| **Q2a. ゴール深度** | ① 分析だけ / ② 分解設計まで / ③ .tfl 生成まで / ④ Cloud に publish & run まで / ⑤ 元フローとの E2E 比較まで | 常に必須 | ④ 以上が publish/run の合意 (以後は自律ループで進む。preflight も goal ≥ ④ でのみ発火)。⑤ は元フローも Cloud 上に存在することが前提 (元 flow LUID 必須) |
| **Q2b. 横断工程** | schedule / repoint (WB / Pulse) / backfill の複数選択 (省略可 = なし)。**repoint の要否が分からなければ空欄で可** — step 0b の consumer probe が旧 output PDS の消費 (WB / Pulse) を実測し、Stop 1 で事実に基づいて提案する | 任意 | migration-planner の `--crosscut` にそのまま渡る (probe の推奨をユーザーが Stop 1 で承認したら repoint を加える)。schedule はトリガ方針 (曜日限定ドメインの有無) の確認が追加で要る ([prep-schedule-designer](../prep-schedule-designer/SKILL.md))。repoint は移行完了済み・旧資産 (WB / Pulse 定義) が Cloud 上に存在することが前提で、schedule とは独立 (片方だけ可)。WB は [prep-workbook-repointer](../prep-workbook-repointer/SKILL.md)、Pulse 定義は [prep-pulse-repointer](../prep-pulse-repointer/SKILL.md) — WB lineage に Pulse 消費は写らないため両方確認する。backfill は計画への事前登載のみで、実行は compare 後の明示要求 + 段取りゲート ([prep-pds-backfiller](../prep-pds-backfiller/SKILL.md)) |
| **Q3. 作業フォルダ名** | `work/<yyyymmdd>_<タグ>/` の `<タグ>` 部分（空欄なら AI が Q1 フロー名から自動生成 → 復唱確認） | 常に必須 | そのセッションの全成果物の置き場 ([§work/ ディレクトリ規約](../../../CLAUDE.md#work-ディレクトリ規約)) |
| **Q4. target path** | publish 先プロジェクトの path（任意深さ可、例: `99_Sandbox/flow241407_decompose`）または target LUID | Q2a が ② 以上で必須（② でも既存 flow 名衝突回避に有用） | step 0a (`get_project_structure.py --project-path`) の入力 |
| **Q5. 既存 migration-plan** | 前セッションで生成した `migration-plan.json` のパス（正準置き場 = 初版セッションの `work/<yyyymmdd>_<tag>/reports/migration-plan.json`） | resume 時のみ必須（新規移行は空欄） | 複数セッションに跨る移行を再開するときに status を manifest 群と突合して再導出 ([prep-migration-planner](../prep-migration-planner/SKILL.md)) |

補足:

- **Q4 が自然言語で来たら path に変換するのはメインエージェントの責務**。手順 (既存階層確認 → 意図復元 → 復唱合意 → 確定 path で step 0a) は prep-extractor 側の解釈レイヤではなく会話で完結
- **`.env` の確認は遅延でよい**: Q2a が ③/④/⑤ または Q1 がサーバー DL のときに必要。step 0a 実行直前に未整備なら聞く
- **復唱 (echo-back) は質問とは別**: Q3 タグ自動生成のように「AI が一度値を決めてユーザーに見せて redirect の機会を与える」のは **no-clarifying-questions モード下でも省略しない**
- URL ID 解決の詳細 (vizportalUrlId からの逆引き等) は [prep-extractor の deploy-context-procedure.md](../prep-extractor/references/deploy-context-procedure.md)
- **複数フロー or Q2b 非空の移行では step 0b で migration-plan を骨生成する** (単発 × Q2b なしは不要)。発動条件と Stop 1 の観点は [prep-migration-planner](../prep-migration-planner/SKILL.md)
- **resume (Q5 あり) では新規 intake を最小化する**: 既存 migration-plan の scope / migration_order / target を追認し、Q1-Q4 は差分だけ確認する

## セッション運用 (速度・トークン)

- **複数フローの移行はバッチをデフォルトにする**: prep-extractor Phase C で依存を把握し、1 セッションに複数フローを載せて deploy-context / 設計パターンを再利用する (単発セッションの繰り返しより 1 フローあたりの実時間・トークンとも大幅に安い)。同一 target なら step 0a / 0b は 1 回で足りる
- **長大セッションの resume / 巻き戻しを避ける**: resume のたびにプロンプトキャッシュが全再構築される。フロー(バッチ)ごとに新セッションを開始し、`deploy-context.md` と `work/` 成果物の再利用で文脈を引き継ぐ (セッション横断の進捗は Q5 の migration-plan.json で追う)
