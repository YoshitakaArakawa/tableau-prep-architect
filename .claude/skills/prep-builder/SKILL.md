---
name: prep-builder
description: prep-architect の decomposition-plan に従って新規 .tfl ファイル群を組み立てる。元 .tfl から該当ノードを抽出し、切れた依存を新規 LoadHyper Input ノードに置換、actions レベル分割があれば SuperTransform を分割、末端に Output ノードを追加して zip 化する。ローカル副作用のみで承認不要。decompose 完了後に設計案を実体ある .tfl に落としたいとき、publish 失敗を受けて .tfl を修正したいときに起動。fork 内で flow_io.py を直接叩いて組み立てるため、大きな元 .tfl JSON のコンテキストは主会話に波及しない。
context: fork
allowed-tools: Read Write Bash(python *) Glob Grep
---

# prep-builder

prep-architect の分解設計案を **実体ある .tfl ファイル群** に落とす Skill。元 .tfl は変更せず、新規ファイル群を `flows/{staging,intermediate,marts}/` 配下に生成する。

ローカル副作用のみで、サーバー副作用は持たない（publish 以降は [prep-deployer](../prep-deployer/SKILL.md)）。

## Caller から渡される入力

`context: fork` で動くため caller (メインエージェント) は会話履歴を渡せない。起動時に以下を文章で明示すること:

| 入力 | 必須 | 例 |
|---|---|---|
| `decomposition_plan_path` | ✅ | prep-architect が出力した `decomposition-plan-<flow>.md` のパス |
| `source_tfl_path` | ✅ | 元の `.tfl` / `.tflx` (ノード定義の抽出元) |
| `deploy_context_path` | publish を見据えるなら | `deploy-context.md`。Output ノードの `projectLuid` 決定に使う |
| `output_dir` | ✅ | 新 .tfl 群の出力先。**正しい値**: (A) このリポ内で作業中なら `work/<yyyymmdd>_<tag>/flows/`、(B) ユーザー Prep プロジェクトで使うなら `<your-prep-project>/flows/`。詳細は [CLAUDE.md §成果物の置き場](../../../CLAUDE.md#成果物の置き場) |

### output_dir ガード (必須)

Skill 起動時、`output_dir` が **このリポジトリ直下の `flows/`** (= `<this-repo>/flows/`) を指していた場合は、組み立てを開始せず以下を返して停止する:

```
ERROR: output_dir=<repo>/flows/ はこのリポジトリでは禁止 (Skill 配布専用リポ、データ実体は追跡対象外)。
正しい置き場:
  (A) このリポ内で作業中: work/<yyyymmdd>_<tag>/flows/
  (B) ユーザー Prep プロジェクトで使用中: <your-prep-project>/flows/
詳細: CLAUDE.md §成果物の置き場
```

判定: `output_dir` の絶対パスが repo root (`.git` を持つディレクトリ) と同じ親で、末端が `flows` の場合。`work/.../flows/` (= repo root の子の `work/` の子) は OK。

### context (B) ユーザー Prep プロジェクトの想定構造

```
<your-prep-project>/
├── .env                       ← 認証情報 ([prep-deployer/references/authentication.md](../prep-deployer/references/authentication.md))
└── flows/
    ├── staging/               # stg_*.tfl
    ├── intermediate/          # int_*.tfl
    └── marts/                 # fct_*.tfl / dim_*.tfl / rpt_*.tfl
```

`flows/` が存在しない場合は本 Skill が作成する。命名規約 (`stg_` / `int_` / `fct_` / `dim_` / `rpt_` プレフィックス) は [references/naming-conventions.md](../../../references/naming-conventions.md)。

## 入力 / 出力

| 項目 | 内容 |
|---|---|
| 入力 | `decomposition-plan-<flow>.md`（prep-architect の出力）＋ 元 .tfl / .tflx |
| 出力 | `flows/{staging,intermediate,marts}/*.tfl` |
| 副作用 | ローカルファイル生成のみ |
| 承認 | 不要（既存ファイルへの上書き時のみ警告して確認） |

## 手順

詳細手順は [references/build-recipe.md](references/build-recipe.md) を参照。要約：

1. 設計案 markdown をパース（含めるべき元ステップ ID / レイヤ / Inputs / Outputs / actions 分割指示）
2. 元 .tfl を [scripts/flow_io.py](../../../scripts/flow_io.py) の `load_flow_json` で展開
3. 各新 .tfl ごとに:
   1. 該当ノードと接続を抽出
   2. 切れた依存を新規 LoadHyper Input ノードに置換
   3. actions 単位の分割があれば SuperTransform の `beforeActionAnnotations` を振り分け
   4. 末端に Output ノード (`WriteToHyper` / `PublishExtract` / `WriteToDatabase`) を追加
   5. zip 化して .tfl として保存
4. 生成サマリをユーザーに報告

組み立てロジックは LLM が flow_io.py を直接呼んで実行する。.tfl JSON のスキーマ・組み立てパターンは [references/tfl-json-schema.md](../../../references/tfl-json-schema.md) 参照。

## 検証

build 完了後、まず **自動チェック** を行う:

- 各新 .tfl の zip 内に `flow` + `maestroMetadata` (+ `displaySettings`) が含まれているか (`load_aux_entries(path)` で確認)
- cross-layer Input ノードが `LoadSqlProxy` で、トップレベル `connections` / `dataConnections` / `connectionIds` / `dataConnectionIds` に該当 entry があるか (`add_pds_input` で作っていれば自動的に揃う)
- 全 Output が `PublishExtract` で、`projectLuid` が deploy-context の layer LUID と一致するか

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
| prep-deployer publish 中の HTTP エラー（Output ノード設定不正等） | 本 Skill に戻って .tfl 修正 |
| prep-deployer run で finishCode=1（Input 接続不可・スキーマ不一致） | 本 Skill で Input ノード書き換え → 再 publish |
| 件数不一致（actions 分割の不備） | prep-architect の decompose に戻ることもあり |

## 制約

MVP では以下を **しない**：

- 自動マイグレーション（Tableau Cloud 上での仮想接続作成等）
- DB View の自動生成・自動デプロイ
- Calculated Field の自動定義（Tableau Desktop での手動設定）

本 Skill の責務は **設計案を実体ある .tfl ファイル群に落とす** ところまで。

## 設計原則

- 元 .tfl は本 Skill では絶対に変更しない（新規ファイルとして書き出す）
- 既存 `flows/` 配下の同名ファイルは上書きしない（警告してユーザー確認）
- 生成した各 .tfl は Tableau Prep Builder で単体動作可能であること
- 生成 .tfl は **必ず元 .tfl の `maestroMetadata` (推奨: `displaySettings` も) を同梱** する (詳細は [references/build-recipe.md](references/build-recipe.md) Step 2 / 4)
- cross-layer Input は **LoadSqlProxy + PDS** で組む (LoadHyper は Cloud 上で繋がらない)。`flow_io.add_pds_input` が Server 接続 / dataConnection / node 登録を一括化、Server 接続を dedup (KB 005232681 重複回避)
- 全 Output は **PublishExtract → 同レイヤ project** で組む (`projectLuid` は preflight 後の deploy-context.md から取得)
- 失敗したらその .tfl の生成を中断、ユーザーに報告（自動回避しない）
