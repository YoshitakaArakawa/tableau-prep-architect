---
purpose: intermediate 層の分解戦略（1 entity 1 .tfl 原則と例外条件）
fetched_at: 2026-05-17
note: 1 SuperTransform を複数 .tfl に分けるか／step 単位で連鎖分割するかの判断基準と命名・注意点
---

# intermediate-decomposition

intermediate 層をどう設計するかのガイド。**prep-architect の decompose 専用** の判断基準。

## 原則: intermediate は 1 entity 1 .tfl にまとめる

長大フローを分解する場合でも、**intermediate は原則 1 entity 1 .tfl にまとめる**。step 単位の連鎖分割（`int_*_step1_*.tfl`, `_step2_*.tfl`, ...）は **例外** として扱う。

理由:
- 1 .tfl 内に閉じれば Tableau Prep Builder で全ロジックを一望でき、レビュー・引継ぎが容易
- ステップ間の中間 Hyper を介さないので I/O オーバーヘッドが無い
- publish 順序・Linked Tasks 連鎖の設定が要らない
- 修正時に「どの step .tfl に手を入れるか」を考えなくて済む（凝集が高い）

## 例外: 連鎖分割が必要なケース

以下のいずれかに該当する場合のみ、step 単位で分割する:

| ケース | 例 |
|---|---|
| 1 .tfl のノード数が極端に多い（30+ ノード目安） | 大規模な業務ロジックの集積体 |
| 中間結果を別の .tfl からも Input として参照したい | step1 の出力 Hyper を別 entity の int でも使う |
| 業務的に明確な責務境界があり、別チームが別々にメンテする | 整形担当チーム vs 集計担当チーム |

これに当てはまらない「規模がそこそこある」程度のものは 1 .tfl 内で構造化するのを優先する。

連鎖分割する場合の命名 ([../../../../references/naming-conventions.md](../../../../references/naming-conventions.md) 参照):

```
int_<entity>_step1_<verb>.tfl     (フィルタ・初期整形)
int_<entity>_step2_<verb>.tfl     (顧客・商品との JOIN)
int_<entity>_step3_<verb>.tfl     (売上区分・優良顧客フラグ等)
```

連鎖の各段の出力（Hyper）が次段の Input。

## レイヤ別の切り出し難度

| レイヤ | 切り出し難度 | 理由 |
|---|---|---|
| staging | 易 | 1 ソース 1 ファイル、型がほぼ固定で機械的に切り出し可 |
| marts | 中 | 出力ノードから逆引きで境界明確 |
| **intermediate** | **難** | 業務ロジックの集積地、複雑な変換、判断要素が多い → AI 支援＋人間判断の主戦場 |

intermediate の難しさは「分割するか否か」だけでなく、「1 .tfl 内をどう構造化するか」「actions レベルでどこを stg / mart へ動かすか」の判断にも及ぶ。

## actions 単位の分割（intermediate 内の話とは別）

1 つの SuperTransform ノードが複数レイヤに跨る actions を持つ場合、**レイヤ境界に沿って** actions を振り分ける。これは intermediate 内部の細分割ではなく、stg / int / mart の責務分離。

例: 「Clean 1」が `Rename×4`（stg 相当の単純整形）と `ROW_NUMBER LOD`（int 相当の Window 計算）を 1 ノードに同梱しているケース → Rename を stg 側 .tfl に、LOD を int 側 .tfl に。

詳細は [../../../../references/layer-responsibilities.md](../../../../references/layer-responsibilities.md) の actions レベル分析節、書式は [../../../../references/decomposition-plan-format.md](../../../../references/decomposition-plan-format.md) の `Actions-level splits` セクションを参照。

## 注意点

- **連鎖分割は最後の手段**: 「分けた方がきれい」程度の理由では分けない
- **意味のかたまりを優先**: 業務的に 1 つの「列追加群」（例: 損益計算 AddCol×9）は 1 .tfl 内に保つ
- **過剰分割を避ける**: 「将来必要かも」と細切れにしない。必要になってから再分解

## Union ノードは削除候補にしない (hard rule)

「actions=0 の Clean を挟んだ self-Union (= `A → Union ← Clean(0 actions, =A)`)」のような構造を見つけても、Union ノードを **削除候補にしてはならない**。Tableau Prep の Union ノードは入力起源を識別する **`Table Names` 列を暗黙的に注入する** ため、下流が `Table Names` を参照 (RemoveColumns / 計算式 / Join clause) しているなら、Union を消すと run 時に `Can't find field 'Table Names'` で失敗する。

判断手順:

1. 削除提案候補のノードに SuperUnion が含まれていたら **無条件に却下**
2. Union の no-op 化 / 統合の理由付けが「actions が空」「両入力が同一」だけなら、それは **schema 等価ではない** (`Table Names` 列の有無で違う)
3. もし本当に省略したいなら、下流全てで `Table Names` への参照が無いことを source DAG 全体で確認したうえで、削除でなく **明示的な AddColumn(Table Names = "")** などの代替に置き換える形でしか提案しない (現実的にはレアケース)

一般化: **Union を含むサブグラフを「pass-through」と判定する根拠を schema レベルで証明できない限り、構造は保持する**。同じ罠は Join の Left/Right namespace / 各 actionNode が持つ implicit field でも起きうるので、「no-op に見える」という直感だけで畳まない。
