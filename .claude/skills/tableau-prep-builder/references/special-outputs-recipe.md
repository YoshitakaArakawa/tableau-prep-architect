---
purpose: tableau-prep-builder の条件付き Output 加工 — mart の Rename-back ノード挿入と incremental/append 継承
note: plan に Rename-back 表がある mart、または元フローが incremental/append のセッションでのみ読む
---

# special-outputs-recipe (Step 3d-2 / 3d-3)

## Step 3d-2: mart の Rename-back ノード挿入 (Output mapping に行を持つ mart のみ)

plan の mart entry に `Rename-back (presentation rename)` 表がある場合 ([decomposition-plan-format.md §Rename-back](../../../../references/decomposition-plan-format.md))、**最終変換ノードと PublishExtract Output の間に専用 SuperTransform を 1 つ挿入**する。既存最終ノードへの actions 追記は列削除順序の罠を踏みうるため、独立ノードが安全。

```python
from flow_io import make_rename_supertransform

rb = make_rename_supertransform(
    renames=[("ticker", "銘柄"), ("settlement_date_買付", "約定日_買付")],  # plan の表の行順
    name="Rename-back",
)
new_flow["nodes"][rb["id"]] = rb
# wiring: <最終変換ノード> → rb → out_node (すべて Default edge)
add_edge(last_node, rb["id"])
add_edge(rb, out_node["id"])
```

verify: Rename-back 適用後の出力列集合が plan の Rename-back 表の `original name` を全て含み、`internal name` を 1 つも含まないこと (内部名の露出ゼロ)。

**⚠️ 出力 PDS シェルの凍結**: Prep が作る出力 PDS の .tds field メタデータは **PDS 初回作成時のスキーマで凍結され、以後の flow run では更新されない** (run は hyper 側のデータ/スキーマのみ更新)。新規 build なら初回 run 時点で rename-back 済みスキーマになるので問題ないが、**publish 済み PDS が存在する状態で後から出力スキーマを変える修正** (rename-back の後付け等) をすると、シェル (= Catalog / Metadata API が読む field list) と物理 hyper が乖離する。データサーバー経由の消費者 (Workbook / 下流 Prep) は hyper 側の正しいスキーマを見るが、Metadata API 検証は偽 FAIL になる。対処: PDS を削除して flow run で作り直す (LUID/content_url が変わる) か、full .tdsx を DL → .tds 内の旧名を書換 → Overwrite republish でシェルだけ直す (LUID/content_url 保持)。

## Step 3d-3: incremental refresh / append 出力の継承 (元フローが incremental の場合のみ)

plan の該当 .tfl に「incremental 継承方針」がある場合 (decompose-self-check 項目 16、flow-summary.md の Meta `Incremental inputs` / `Append-mode outputs` が一次シグナル)、元フローの refresh 設定を新 .tfl に焼き込む。**継承層は既定 int** (層の判断基準は decompose-self-check 項目 16)。watermark 追跡は同一 .tfl 内の Input/Output ペアでのみ機能するので、`input_node_id` と `output_node_id` は継承層の同一 .tfl に置く。

```python
from flow_io import set_incremental_refresh

set_incremental_refresh(
    new_flow,
    input_node_id=<incremental input の LSP node id>,   # 元フローで incrementalEnabled=true だった入力に対応
    control_field="Date",                                # 元 IncrementalConfiguration.controlFieldName の caption
    output_node_id=out_node["id"],
    output_field="Date",                                 # 出力側 control 列 (通常 control_field と同名)
    is_incremental_default=True,                          # REST /run に runMode 引数が無いので default を incremental に
)
```

これは `flow["nodeProperties"]` に `IncrementalConfiguration` (入力) + `OutputRefreshOptions` (出力、append/append) を書き込む。build ヘルパは append/append のみ対応 (実証済みの組合せに限定)。

**運用上の重要な注意**:

- **run 規律** (append 出力を full run に当てると多重化する / 初回だけ full run で baseline → 以後 `run_flow.py --incremental`) は publish フェーズの正典 [publish-recipe.md §append / incremental フローの run 規律](../../tableau-prep-deployer/references/publish-recipe.md) に従う。ここでは build 側の含意のみ記す
- 元 PDS が過去の累積履歴を持つ場合、それは現ソースに残っていないので継承層 (既定 int) の新 PDS には初回 baseline 分しか入らない。**build 時の既定は baseline-forward** (旧 PDS はアーカイブ残置、plan 項目 16)。履歴 seed が必要なら移行後の別工程 [tableau-pds-backfiller](../../tableau-pds-backfiller/SKILL.md) で行う (build には含めず、ユーザー明示要求 + ゲート付き)
