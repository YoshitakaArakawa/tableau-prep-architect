---
purpose: prep-architect decompose 完了後のユーザー確認 (Stop 2) における観点を 3 Tier で規定する。Tier 1 は明示確認必須、Tier 2 はデフォルト受諾、Tier 3 は Agent 自律で Stop 2 に出さない
note: Stop 2 で何を必ずユーザーに見せて、何を黙示で通すか、何を Agent 内部で完結させるかの責務分担を定義する。decomposition-plan-format.md と decompose-self-check.md と組合せて使う
---

# review-checkpoints

`decomposition-plan-<flow>.md` + `.html` を生成した後、prep-builder に渡す前に **1 回だけユーザー確認 (Stop 2)** を取る。提示は **`.html` のパスを案内してブラウザで開いてもらう** のを主とする (As-is → 分解先マップと依存 DAG が視覚確認できる。md は git 追跡の設計記録 + ターミナル fallback)。本ファイルはその確認で何を見せ何を省くかの観点を 3 Tier で定義する。

レビュー疲労を避けるため `Tier 1` (明示確認必須) / `Tier 2` (デフォルト受諾、異論時のみ修正) / `Tier 3` (Agent 自律、Stop 2 では出さない) に層別する。ユーザーは Tier 1 だけ見て `OK` で進めれば良く、必要なら Tier 2 にも踏み込める。

## Tier 1 — 明示確認必須

ユーザーから `OK` または `<行番号> <修正指示>` の応答が来るまで prep-builder に進まない。

| # | 項目 | 単位 | 修正指示の粒度 | 根拠 |
|---|---|---|---|---|
| 1 | **新 .tfl 命名** (`stg_*` / `int_*` / `fct_*` / `dim_*` / `rpt_*`) | .tfl 単位 | 行ごと rename | publish 後の PDS 名 = BI 側参照名。変更は rebuild + republish になり巻き戻し高 |
| 2 | **レイヤ配置** (各 .tfl の stg / int / mart 判定) | .tfl 単位 | 行ごと layer 変更 | 業務的解釈 (集約粒度、再利用性) が絡む。業務知識依存パラメータは auto-detect 禁止 |
| 3 | **Input policy** (各 Input の `passthrough` / `augment` / `needs_provisioning`) | Input 単位 | 行ごと policy 変更 | 整形済 PDS か raw データかは AI 判定不能 |
| 4 | **整備依頼リスト** (direct_db / extract Input → vconn 化 / PDS 化案) | Input 単位 | 「整備します」「partial build で進める」を選択 | session 進行判断。整備未完で build に進むと当該 stg は skip される |
| 5 | **Output mapping** (元 output PDS → 分解後 output PDS の対応) | output 単位 | 統合 / 分割の指示 | parity 比較 (prep-output-comparator) の土台。mart 出力列名は元 output PDS と完全一致が規範だが、命名レジーム (元名 end-to-end 保持、[../../../../references/input-policy.md §命名レジーム](../../../../references/input-policy.md)) の下では自動達成される — 確認対象は対応関係のみ |

### ユーザー応答の解釈

| 入力 | 解釈 |
|---|---|
| `OK` | Tier 1 全項目受諾、build に進む |
| `<行番号> <修正指示>` (例: `Input #2 policy → passthrough`) | 該当行のみ修正、他は受諾 |
| `partial` | needs_provisioning がある場合、整備未完でも build を試みる選択 |

応答後、main agent は `decomposition-plan-<flow>.json` の該当フィールドを修正し `render_plan_md.py` で md + html を再レンダリングして、prep-builder を起動する (md / html を直接編集しない。確認の二重ループは作らない)。修正が広範に及ぶ場合は本 Skill を `mode=decompose` で再起動する。

## Tier 2 — デフォルト受諾、異論時のみ修正

提案として plan に書き出すが、ユーザーが触れなければそのまま通す。Stop 2 を `OK` だけで通せる前提。

| # | 項目 | デフォルト挙動 |
|---|---|---|
| 6 | **命名レジーム** (列名の扱い) | 元の内部名を end-to-end 保持 ([input-policy.md §命名レジーム](../../../../references/input-policy.md))。stg transforms はピン留めのみ、rename_back は空。英語化の要望が来たら「列参照 rewriter 非実装のため非対応 (mart より下流の caption / Workbook 側で対応)」と説明する |
| 7 | **Materialization** (`live_pds` / `tfl` / `passthrough`) | Input `kind` から自動決定 (vconn → live_pds、pds → passthrough、複雑 stg のみ tfl) |
| 8 | **Target project layout** (`target/flows/stg`, `target/datasources/stg` 等) | [../../../../references/project-hierarchy.md](../../../../references/project-hierarchy.md) に従う |
| 9 | **Migration order** (段階順序) | stg → int → marts の機械順 |
| 10 | **cast 提案** (string → numeric 等) | 「型表記の明らかな不整合」だけ提案、それ以外は提案なし |
| 11 | **hide 提案** (下流未参照列) | flow-summary.md から判定可能なもののみ提案 |
| 12 | **Actions-level splits** (1 SuperTransform の複数 .tfl 分割) | [intermediate-decomposition.md](intermediate-decomposition.md) のルールで自動判定、結果のみ表示 |
| 13 | **Description** (各 .tfl の 1-2 行解釈) | AI 生成、誤りがあれば指摘 |

### Tier 2 が Tier 1 に昇格するケース

以下は plan 上で目立つように明記して、ユーザーが見落とさないようにする (ただしデフォルト受諾の運用は変えない):

- Materialization=tfl が出た .tfl: passthrough/augment に収まらない非標準ケース。理由を Description に 1 文で書く
- Joins cardinality が `不明` の SuperJoin: 元 .tfl から判定不能だったサイン

## Tier 3 — Agent 自律、Stop 2 では出さない

[decompose-self-check.md](decompose-self-check.md) の 17 項目は原則ここに入る。machine-verifiable で、ユーザーが判断する材料を持たない (元 .tfl の Prev チェーンを記憶していない、SuperUnion の暗黙列を知らない等) ため、ユーザーに見せず Agent 内部で潰す。

例外として self-check 由来でも Stop 2 に露出するもの: 項目 13 の live_pds cast/hide 未検証リスク、項目 15(b) の既存 stg 再利用案 (提案として提示)、項目 16 の incremental 継承方針 (Tier 1 で明示確認)。divergent forward rename を導入した例外ケースの Rename-back 表は Tier 1 #5 の Output mapping と合わせて提示する (項目 14 が検証するのはレジーム順守とカバレッジ)。

prep-builder の `verify_lineage_closure` / `verify_edge_namespaces` で二重防御も効いているため、decompose 段階で潰しきれなくても build で機械的に弾かれる。

## Tier 1 / Tier 2 の境界判断

ある観点を Tier 1 に置くか Tier 2 に下げるかの判断基準:

| 質問 | YES なら Tier 1、NO なら Tier 2 |
|---|---|
| 業務知識・ドメイン文脈に依存するか | YES → Tier 1 (auto-detect 禁止) |
| 後から修正すると republish + 下流 BI 影響が広いか | YES → Tier 1 (巻き戻し高) |
| 機械的ルールから一意に導けるか | YES → Tier 2 (自動決定) |
| ユーザーがすぐ判断できる粒度か | NO → Tier 3 (Agent 内部) |

新しい観点を追加するときは本表で位置を決めてから plan / SKILL.md に反映する。
