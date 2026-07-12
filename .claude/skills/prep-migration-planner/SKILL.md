---
name: prep-migration-planner
description: 複数フロー移行または横断工程 (スケジュール設計 / Workbook 参照置換 / PDS backfill) を含む Prep 分解プロジェクトで、scope・移行順・人間作業キュー・進捗を 1 枚に集約する移行計画書 (migration-plan.md + migration-plan.json) を生成し、工程の進行に合わせて更新する Skill。prep-extractor Phase C の後 (step 0c) に骨を作って Stop 1 でユーザー承認を取り、以降は各工程完了時に main agent が status と決定を埋めていく progressive-fill 台帳で、セッション横断の resume state も兼ねる。ユーザーが「移行計画を作って」「計画書を出して」「移行の段取りを整理して」と言ったとき、または goal が Cloud publish 以降で横断工程を含む・対象フローが複数のときに起動する。フロー内設計 (命名 / レイヤ / Input policy) には踏み込まない (それは prep-architect の decomposition-plan が正)。Cloud 副作用なし・ローカルのみ。
---

# prep-migration-planner

end-to-end 移行 (extract → decompose → build → publish → compare → schedule → repoint → backfill) を**プロジェクト単位でオーケストレーションする台帳** `migration-plan` (`.json` + `.md`) を生成・更新する Skill。**プロジェクト全体の割り付けと進捗**を扱い、フロー内設計 (命名・レイヤ・Input policy・Output mapping) には踏み込まない — それは `prep-architect` の `decomposition-plan-<flow>.json` が正 (§役割境界)。設計モデルは [references/orchestration-model.md](references/orchestration-model.md)、計画書のスキーマと md テンプレートは [references/plan-format.md](references/plan-format.md)。

本 Skill は `context: fork` を **付けない**。理由は Stop 1 のユーザー承認・intake 回答の合成・後段への決定の受け渡し (courier) を**主会話コンテキストで**扱うため (`prep-deployer` / `prep-pds-backfiller` が承認・失敗観測のため fork しないのと同じ)。ただし本 Skill は**サーバー副作用が無く、生成物はローカルの `migration-plan.*` のみ**という点で書き込み系と異なる。fork しないので `## Timing` ブロックは返さない (主会話が内部時間を直接観測できる)。

役割対称性の第 3 類型: 読み取り = prep-extractor + comparator + schedule-designer + workbook-repointer / 書き込み = prep-deployer (+ augmenter, backfiller) / **オーケストレーション = prep-migration-planner** (ローカル台帳のみ、決定を前方へ配る唯一のセッション横断 artifact)。

## いつ呼ばれるか

- **位置**: `step 0b` (`prep-extractor` Phase C の直後、`prep-architect` の analyze/decompose より前)。
- **発動条件**: 忘れ防止の価値は **goal 段階**に、順序管理の価値は**フロー数**に依存する。この 2 軸で切る:

| | goal ①〜⑤ (横断工程なし) | goal ⑥/⑦ or backfill 意図あり (横断工程あり) |
|---|---|---|
| **単発フロー** | 作らない | **作る** (human_queue 中心の薄い版) |
| **複数フロー** | 作る (scope / order / batch 管理) | 作る (フル) |

「単発 × ⑤以下」だけが非作成ゾーン (⑤ compare までは Agent 自律で人間作業が無い)。⑥ (schedule) と ⑦ (repoint) は独立に選べる (片方だけのケースがある)。

- **Stop 1 (薄い)**: init 直後に計画書初版を提示し**異論を受けるだけ**。重い明示確認は実質無い (scope は intake の追認、migration_order は機械導出の追認)。一気通貫を止めない。ユーザー応答は `OK` か `<セクション> <修正>`。
- **Stop 1 と Stop 2 の境界**: Stop 1 = **プロジェクト割り付け** (scope / 順序 / バッチ / 横断工程の適用 / 人間作業の段取り)。Stop 2 = **フロー内設計** (`prep-architect` の `decomposition-plan`)。詳細は [references/orchestration-model.md](references/orchestration-model.md)。
- **skip 動線**: 複数フローでもユーザーが「計画不要ですぐ進めて」と言えば main agent は skip してよい (副作用が無いので手動ゲートは不要)。

## 何をするか — 3 局面 (progressive fill)

計画書は「冒頭で完成」ではなく、Workflow の各工程が該当セクションを埋めていく。本 Skill が能動的に動くのは **init 局面**のみ。update / resume は **main agent** が計画書を読み書きする (planner 再起動は不要。大きな作り直しが要るときのみ再 invoke)。

| 局面 | 実行主体 | 動作 | 埋まる / 更新されるセクション |
|---|---|---|---|
| **init** (step 0b) | 本 Skill | facts + deploy-context + intake から骨を生成 → Stop 1 提示 | scope / migration_order / backfill_candidates / human_queue 骨 / pointers |
| **update** (decompose〜横断工程) | main agent (courier) | 各工程完了で status 更新、横断工程直前に決定を埋め、同値を下流 Skill の既存引数に渡す | matrix.rows (build 開始で生成) / trigger_policy / old_schedule_notes / backfill mode / repoint 対応 / 各 status |
| **resume** (新セッション冒頭) | main agent | `migration-plan.json` を読み、status を manifest 群と突合して再導出、次工程を決定 | status (再導出) / pointers.manifests 追記 |

## init の実行手順

進捗:

- [ ] Step 1: 骨 JSON を生成 (`init_plan.py`)
- [ ] Step 2: md にレンダリング (`render_migration_plan.py`)
- [ ] Step 3: Stop 1 提示 (md のパスを案内し、scope / order / backfill 候補 / human_queue を要約)

### Step 1: 骨 JSON を生成

複数フロー (Phase C の `--json` facts を消費) か単発 (生フロー 1 本を直読) かで入力が分岐する。

```bash
# 複数フロー: map_flow_dependencies.py --json の facts (incremental 列込み) を読む
python ${CLAUDE_SKILL_DIR}/scripts/init_plan.py \
  --flow-deps-json <output_dir>/flow-dependencies.json \
  --flow-deps-md <output_dir>/flow-dependencies.md \
  --deploy-context <output_dir>/deploy-context.md \
  --goal <1-7> --target "<target path>" --flow-count multi \
  --scope-in "<flowA>,<flowB>,<flowC>" [--scope-out "<flowD>"] \
  --crosscut "schedule,repoint" \
  [--session-batch "<tag1>:<flowA>,<flowB>" --session-batch "<tag2>:<flowC>"] \
  --out <output_dir>/migration-plan.json

# 単発フロー: 生フロー 1 本を直読 (backfill 検出のため)
python ${CLAUDE_SKILL_DIR}/scripts/init_plan.py \
  --flow <flow.json/.tfl> \
  --goal 7 --target "<target path>" --flow-count single \
  --scope-in "<flow>" --crosscut "repoint" \
  --out <output_dir>/migration-plan.json
```

- `--goal` は 1-7 の整数 (Q2 段階)。meta には表示ラベルが入る。
- `--crosscut` は human_queue を組む横断工程 (`schedule` / `repoint` / `backfill` を任意個)。⑥⑦独立のため goal 整数からは導かず**明示指定**する。backfill 候補が検出されれば `backfill` は自動で human_queue に加わる。
- backfill 候補は facts の incremental 列 (複数) または `get_incremental_config` (単発) から機械抽出される (`flow_io` の正準ロジック)。
- scope / target / goal / batch は intake 由来。main agent が会話から確定して渡す (self-check は Stop 1 でユーザーが行う)。

### Step 2: md にレンダリング

```bash
python ${CLAUDE_SKILL_DIR}/scripts/render_migration_plan.py \
  --plan <output_dir>/migration-plan.json -o <output_dir>/migration-plan.md
```

`.json` が正、`.md` はレンダリング (`decomposition-plan` と同じモデル — md を手編集しない)。single/multi でテンプレート分岐、nullable 未充填は `(＜工程＞で確定)` プレースホルダ表示、`matrix.rows` があれば格子を描画する。

### Step 3: Stop 1 提示

md のパスを案内し、**scope / migration_order / backfill 候補 / human_queue の骨**を要約して提示する。ユーザー応答 (`OK` / `<セクション> <修正>`) を待ち、修正があれば `--...` 引数を直して Step 1-2 を再実行する (md を直接編集しない)。`OK` で decompose へ進む。

## update / resume (main agent の責務)

本 Skill は関与しない。main agent が `migration-plan.json` を直接読み書きする。要点 (詳細は [references/orchestration-model.md](references/orchestration-model.md)):

- **courier パターン**: 下流 Skill を計画書 artifact に結合させない。例 (schedule 工程) — main agent が ①ユーザーにトリガ方針を聞く ②`trigger_policy` を埋める ③**同値を `prep-schedule-designer` の既存 `trigger_policy` 引数に渡す** ④完了後 `human_queue` の `runbook_ref` と matrix status を更新。同型を repoint (`manifest_paths` 集約) / backfill (起動判断リスト) に適用。
- **status は再導出キャッシュ**: resume 時に manifest 群と突合。正本にしない。
- **決定台帳 ≠ ファクトキャッシュ**: 決定 (trigger_policy 等) は計画書が正。ファクト (run-type / LUID / 依存エッジ) は下流が `.tfl` 実体・manifest・flow-dependencies.md から毎回再導出する — 計画書からは読ませない。

## 役割境界 (何を持たないか)

- **フロー内設計を持たない**: 命名・レイヤ配置・Input policy・Output mapping は `decomposition-plan-<flow>.json` (`prep-architect`) が正本。`prep-architect` / `prep-builder` に決定を配らない (両者は `flow-dependencies.md` を直接消費する)。
- **matrix は decompose 後**: 行 = 分解後 .tfl 名は decompose の産物で init 時に存在しない。init では matrix は空章、build 開始時に分解後 .tfl 単位で格子が生える ([plan-format.md](references/plan-format.md))。
- session manifest との分担: `publish-manifest.json` はセッション内 publish/run 状態、`migration-plan` はセッション横断のオーケストレーション状態 (schedule/repoint/backfill 進捗)。二重管理ではない。

## 失敗時の動作

スクリプトが失敗したら停止して caller にエラーを返す (Cloud 副作用が無いのでリトライ判断は caller)。よくある失敗:

- `--flow-deps-json` が無い / パース失敗: Phase C 未実行または渡し漏れ。複数フローなら `map_flow_dependencies.py --json` を先に実行するよう返す。
- backfill 候補が空: incremental 未検出なら正常 (候補なしで進む)。フラグ列が facts に無い場合は `map_flow_dependencies.py` が incremental 対応版か確認する。
- 単発なのに `--flow` が無い: 生フローパスを渡すよう返す。

## 依存

- Python: 標準ライブラリのみ (json / pathlib / datetime / zipfile)。incremental 検出は repo 共通 `scripts/flow_io.py` (`get_incremental_config`) を import する。生フロー読み込みは bare .json も扱う自前ローダで行う (`flow_io.load_flow_json` は .tfl/.tflx 専用のため)。
- 複数フロー時は `prep-extractor` Phase C の `map_flow_dependencies.py --json` 出力 (incremental 列込み) を前提とする。
- Cloud アクセス無し (ローカル完結、認証不要)。
