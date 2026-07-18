---
name: tableau-prep-builder
description: tableau-prep-architect の decomposition-plan に従って新規 .tfl ファイル群を組み立てる。元 .tfl から該当ノードを抽出し、切れた依存を新規 LoadSqlProxy Input ノード (上流 PDS 参照) に置換、actions レベル分割があれば SuperTransform を分割、末端に Output ノードを追加して zip 化する。ローカル副作用のみで承認不要。decompose 完了後に設計案を実体ある .tfl に落としたいとき、publish 失敗を受けて .tfl を修正したいときに起動。
context: fork
model: sonnet
allowed-tools: Read Write Bash(python *) Glob Grep
---

# tableau-prep-builder

tableau-prep-architect の分解設計案を **実体ある .tfl ファイル群** に落とす Skill。元 .tfl は変更せず、新規ファイル群を `flows/{staging,intermediate,marts}/` 配下に生成する。

ローカル副作用のみで、サーバー副作用は持たない（publish 以降は [tableau-prep-deployer](../tableau-prep-deployer/SKILL.md)）。

## Caller から渡される入力

`context: fork` で動くため caller (メインエージェント) は会話履歴を渡せない。起動時に以下を文章で明示すること:

| 入力 | 必須 | 例 |
|---|---|---|
| `decomposition_plan_path` | ✅ | tableau-prep-architect が出力した `decomposition-plan-<flow>.json` のパス ([plan-json-schema.md](../../../references/plan-json-schema.md))。.md しか無い旧セッションは §旧 md プランの扱い |
| `source_tfl_path` | ✅ | 元の `.tfl` / `.tflx` (ノード定義の抽出元) |
| `output_dir` | ✅ | 新 .tfl 群の出力先。**正しい値**は `work/<yyyymmdd>_<tag>/flows/` (`<tag>` は Session intake の Q3)。詳細は [CLAUDE.md §work/ ディレクトリ規約](../../../CLAUDE.md#work-ディレクトリ規約) |
| `only_flows` | 任意 | targeted rebuild (comparator gap 修正) のとき、再 build する flow 名リスト |

### output_dir ガード (必須)

Skill 起動時、`output_dir` が **このリポジトリ直下の `flows/`** (= `<this-repo>/flows/`) を指していた場合は、組み立てを開始せず以下を返して停止する:

```
ERROR: output_dir=<repo>/flows/ はこのリポジトリでは禁止 (データ実体は work/ 配下に隔離、リポ直下は追跡対象の本体のみ)。
正しい置き場: work/<yyyymmdd>_<tag>/flows/
詳細: CLAUDE.md §work/ ディレクトリ規約
```

判定: `output_dir` の絶対パスが repo root (`.git` を持つディレクトリ) と同じ親で、末端が `flows` の場合。`work/.../flows/` (= repo root の子の `work/` の子) は OK。

### 出力先の想定構造

```
work/<yyyymmdd>_<tag>/flows/
├── staging/               # stg_*.tfl
├── intermediate/          # int_*.tfl
└── marts/                 # fct_*.tfl / dim_*.tfl / rpt_*.tfl
```

`flows/` 配下の各レイヤが存在しない場合は本 Skill が作成する。命名規約 (`stg_` / `int_` / `fct_` / `dim_` / `rpt_` プレフィックス) は [references/naming-conventions.md](../../../references/naming-conventions.md)。

## 入力 / 出力

| 項目 | 内容 |
|---|---|
| 入力 | `decomposition-plan-<flow>.json`（tableau-prep-architect の出力、設計の正）＋ 元 .tfl / .tflx |
| 出力 | `flows/{staging,intermediate,marts}/*.tfl` |
| 副作用 | ローカルファイル生成のみ |
| 承認 | 不要。生成 .tfl は plan.json から再現可能な派生物なので、同一セッション `output_dir` への再 build 上書きは許容 (targeted fix の `--only` を含む)。`context: fork` 内からユーザー確認は取らない |

メイン会話への戻り値の末尾に **`## Timing` ブロック** を必ず含める (フォーマットと Skill 別 breakdown 推奨項目: [skill-timing-contract.md](../../../references/skill-timing-contract.md))。

## 手順

**デフォルトは 1 コマンド** — 組み立てロジックの手書き (session `build_tfls.py`) は廃止し、[scripts/build_from_plan.py](scripts/build_from_plan.py) が plan.json から全成果物を機械生成する:

```bash
python ${CLAUDE_SKILL_DIR}/scripts/build_from_plan.py \
  --plan <session>/reports/decomposition-plan-<flow>.json \
  --source <元.tfl> \
  --output-dir <session>/flows \
  --manifest <session>/reports/publish-manifest.json
```

このコマンドが行うこと (詳細な変換規則は [references/build-recipe.md](references/build-recipe.md) と [plan-json-schema.md §配線の導出規則](../../../references/plan-json-schema.md)):

1. plan.json の構造検証 + 元フローとの整合検証 (step 範囲 / total_nodes / 配線可能性 / lineage closure)。エラーなら build せず終了。**placeholder ガード**: `flow_projects` / `ds_projects` に gen_plan_skeleton の TODO placeholder が残る plan は、`--manifest` 指定 (publish 前提) なら build せず fail (preflight → Phase B 再実行 → plan の LUID 更新が必要)。`--manifest` 無しなら WARNING で許容 — goal ③ のローカル build で、生成 .tfl は publish 不可
2. 各 plan entry を kind dispatch:
   - `input_status: needs_provisioning` → **build を skip**、manifest に `status: skipped_pending_provisioning` で登録。下流 int/marts はそのまま build (run 時に該当 stg PDS 不在で fail するのは正常な escalation 経路)
   - `kind: pds_augment` (stg のみ) → `inspect_input_node()` で vconn 再検証 (非 vconn は中断 + escalation)、[tableau-pds-augmenter](../tableau-pds-augmenter/SKILL.md) spec を `flows/staging/<name>.augmenter.json` に emit (.tfl は作らない)
   - `kind: tfl` → ノード抽出・LoadSqlProxy 置換・actions 分割・rename-back 挿入・incremental 設定・Output 追加・zip 化
3. build 後検証: `verify_lineage_closure` + `verify_edge_namespaces` + zip entry チェック (fail なら exit 1)
4. `--manifest` 指定時は [scripts/publish_manifest.py init --plan-json](../../../scripts/publish_manifest.py) で session manifest を初期化。**既存 manifest があれば原則保持** (`init` は exit 1 で安全に止まる)、上書きは `--force-manifest` を明示。再 build では既存 manifest を保ったまま進める
5. LLM は生成サマリを報告 (kind=tfl / kind=pds_augment / skipped_pending_provisioning の件数)

comparator gap の targeted fix は plan.json の該当 entry を修正 → `--only <flow名>` で該当 .tfl だけ再 build する (全体再 build も re-analyze も不要)。

検証エラーや未知の構造 (非変換 Container 等) で build_from_plan.py が中断した場合のみ、LLM が [scripts/flow_io.py](../../../scripts/flow_io.py) primitives / [scripts/build_helpers.py](../../../scripts/build_helpers.py) を直接使って個別対処する (.tfl JSON スキーマは [references/tfl-json-schema.md](../../../references/tfl-json-schema.md))。

### 旧 md プランの扱い

plan.json が無く `decomposition-plan-<flow>.md` しか無い場合 (旧セッションの再開) は、md を読んで plan.json に転記してから build_from_plan.py を使う (スキーマ: [plan-json-schema.md](../../../references/plan-json-schema.md))。md 直読みの手組み build はしない。

## 検証

build 完了後の **自動チェック** は `build_from_plan.py` が build の一部として実行する — `verify_lineage_closure` + `verify_edge_namespaces` + zip entry チェック (`flow` / `maestroMetadata` の同梱)。いずれか fail なら exit 1 で中断する (詳細は [references/build-recipe.md §Step 4.5](references/build-recipe.md))。

その後、ユーザーに **手動検証** を案内する:

1. Tableau Prep Builder で各 .tfl を開く
2. Input ノードがエラーにならないか確認
3. プレビュー実行 → 想定通りの出力スキーマか確認
4. 元フローと比較して数値一致を確認

## 失敗時の戻り先

| 発覚タイミング | 戻り先 |
|---|---|
| build 中の JSON 組み立てエラー | このまま再試行 / 設計案を修正 |
| Prep Builder で開けない（loomVersion 不整合等） | 本 Skill で .tfl 再生成 |
| tableau-prep-deployer publish 中の HTTP エラー（Output ノード設定不正等） | 本 Skill に戻って .tfl 修正 |
| tableau-prep-deployer run で finishCode=1（Input 接続不可・スキーマ不一致） | 本 Skill で Input ノード書き換え → 再 publish |
| 件数不一致（actions 分割の不備） | tableau-prep-architect の decompose に戻ることもあり |

## 制約

MVP では以下を **しない**：

- 自動マイグレーション（Tableau Cloud 上での仮想接続作成等）
- DB View の自動生成・自動デプロイ
- .tfl のフロー論理へ新規ビジネス calc を発明すること（設計は architect、実行は元ノードの転写に限る）

**calc 注入は例外**: Live PDS 化する stg (`kind: pds_augment`) では、plan の Transforms に基づく calc / rename / cast / hide が **augmenter spec 経由で自動注入** される。`build_from_plan.py` が `flows/staging/<name>.augmenter.json` を emit し、tableau-prep-deployer が [tableau-pds-augmenter](../tableau-pds-augmenter/SKILL.md) で適用する（builder 自身は .tfl を作らない）。

本 Skill の責務は **設計案を実体ある .tfl 群 + augmenter spec に落とす** ところまで。

## 設計原則

- 元 .tfl は本 Skill では絶対に変更しない（新規ファイルとして書き出す）
- 生成 .tfl は plan.json から再現可能な派生物。同一セッション `output_dir` への再 build は既存 .tfl を上書きしてよい（targeted fix の `--only <flow名>` を含む）。`context: fork` 内からユーザー確認は取らない
- 生成した各 .tfl は Tableau Prep Builder で単体動作可能であること
- 生成 .tfl は **必ず元 .tfl の `maestroMetadata` (推奨: `displaySettings` も) を同梱** する (詳細は [references/build-recipe.md](references/build-recipe.md) Step 2 / 4)
- cross-layer Input は **LoadSqlProxy + PDS** で組む (LoadHyper は Cloud 上で繋がらない)。`flow_io.add_pds_input` が Server 接続 / dataConnection / node 登録を一括化、Server 接続を dedup (KB 005232681 重複回避)
- 全 Output は **PublishExtract → 同レイヤ project** で組む (`projectLuid` は plan.json の `ds_projects.<layer>.luid` から取得。充填は gen_plan_skeleton が preflight 後の Phase B 再実行で更新済みの deploy-context.md を入力に行う)
- 失敗したらその .tfl の生成を中断、ユーザーに報告（自動回避しない）
