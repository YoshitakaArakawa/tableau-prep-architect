# decompose 完了前 self-check (詳細版)

`decomposition-plan-<flow>.md` を出力する直前に **必ず通す** 14 項目チェック。SKILL.md からは 1 行サマリでしか参照されないため、各項目の **判定基準と典型 anti-pattern** を本ファイルに集約する。

prep-builder の build 開始時にも `verify_lineage_closure` / `verify_edge_namespaces` で機械的に二重防御するが、decompose 段で潰しておくほうがやり直しが安い (build → publish → run fail → rebuild の loop が 10-15 min)。

## 1. Upstream lineage 表

各 .tfl ごとに `Upstream lineage` 表を埋める ([../../../../references/decomposition-plan-format.md](../../../../references/decomposition-plan-format.md) の Lineage closure invariant 節)。各 Included step が「どの宣言 Input から Prev チェーンで到達できるか」を 1 行で書く。

## 2. Prev 連鎖の到達確認

各 Included step は flow-summary.md の Topology 表で Prev 連鎖を辿ったとき、その .tfl の宣言 Inputs に到達すること。**下流の結合キーから逆推定しない** (列が上流 Input に存在せず run 時 `Unknown field name` で失敗)。

## 3. SuperUnion 削除禁止

削除提案ノード一覧に SuperUnion を含めない。Union は actions=0 / 入力ブランチが同一に見えても **削除候補にしてはならない**。

理由: Union ノードは入力起源を識別する `Table Names` 列を暗黙注入し、下流 RemoveColumns(Table Names) や Join clause が依存しているケースがある。Union 周辺の no-op を畳む提案を出す前に、Union 出力スキーマの参照を下流で全洗いしてからにする。

一般化すると「**下流のスキーマ依存を Source DAG 全体で完全に再現できると証明できない限り、Union は保持**」。

## 4. Output mapping セクション

`## Output mapping (original → decomposed)` セクションが埋まっているか。元フローの全 output PDS と、それを引き継ぐ marts レイヤ flow の対応が表で書かれているか。

本表が欠けると prep-builder の `publish_manifest.py init` が失敗し、最終的に prep-output-comparator がペアを組めない ([../../../../references/decomposition-plan-format.md](../../../../references/decomposition-plan-format.md) の Output mapping 節 / [../../../../references/publish-manifest-format.md](../../../../references/publish-manifest-format.md))。

## 5. 型変換 / 名前変換を staging に集中

intermediate / marts レイヤの `Included original steps` に `ChangeType` (型キャスト) や `Rename` (列名変更) を含む actions が残っていたら、staging の責務漏れの疑い。`Actions-level splits` セクションで該当 actions を stg 側に巻き戻すことを検討する。

例外: intermediate での Join 後にしか型が確定しない列 (`derived = TOFLOAT(col_a + col_b)` 等) は intermediate に残して良い。

## 6. 同一ソースの Input 集約

複数の stg .tfl が同一の `Source` (例: `vc_salesforce / Opportunity`) を Input にしていたら統合候補。

判定: `## New .tfl files` の各 stg セクションの `Inputs` を集計し、同じ Source 名が複数 .tfl に出現していないか確認。

例外: 同一テーブルから **異なる列セット** を取り出して別ドメインに供給するケース (`stg_orders__metrics` と `stg_orders__metadata` が同じ `Orders` テーブルから別目的で columns を抜く) は分離保持で良い。

## 7. Dependency DAG Before/After

`## Dependency DAG (Mermaid)` に Before / After 2 ブロックが揃っているか ([../../../../references/decomposition-plan-format.md](../../../../references/decomposition-plan-format.md) の Dependency DAG 節)。

## 8. Joins cardinality

Join を含む .tfl に `**Joins**` フィールドが書かれ、cardinality (1:1 / 1:N / N:1 / N:N / 不明) が記載されているか。SuperJoin ノード、または .tfl 内で Join を行うステップを含む .tfl で必須。**不明な場合も `不明` と明示** (空欄不可、書式詳細は [../../../../references/decomposition-plan-format.md](../../../../references/decomposition-plan-format.md) の Joins field の書式 節)。

## 9. marts AddCol の上流移行検討

marts レイヤに残っている `AddCol` (計算フィールド追加) actions を上流 (intermediate) で実施できないか検討したか。

検討の結果 marts 残置が妥当な場合はそれで良い (例: BI 表示用整形、行単位の派生列でかつ他フローから再利用しないもの)。判定は user 側に判断材料を提示する形で良い。

## 10. marts Filter の上流移行検討

marts レイヤに残っている Filter actions を上流 (intermediate / staging) で実施して行数削減を前倒しできないか検討したか。

検討の結果 marts 残置が妥当な場合はそれで良い (例: marts 固有のサンプリング・上位 N 件、後段で full history が必要)。

## 11. 列削除 actions の元順序保全

元 .tfl の各 SuperTransform 内で「列を削除する action (RemoveColumns、または column を消す副作用を持つ他 action) と、その列を参照する他 actions」の前後関係を、分解後の .tfl 間 / .tfl 内で逆転させていないこと。

stg / int に actions を分割するとき、列削除 action を上流 .tfl に置いたら、その削除列を参照する下流 actions が同じ .tfl 内かさらに上流に到達できなければならない。

参照とは:
- FIXED LOD の partition key
- ORDERBY
- AddColumn 式の参照列
- Filter 式の参照列
- Join clause の結合キー
すべて。

判定方法: 各列の最終削除 step を topological 上で特定し、その列名を文字列マッチで参照する全 step が削除 step より「上流」にあるか確認。逆転していたら、列削除 action を下流 .tfl 側に移すか、分割境界をずらす。

例外なし (`Table Names` 等の Union 暗黙注入列も同じ精神で扱う、項目 3 と独立に判定)。

## 12. 分岐ノード下流の列要件チェック

flow-summary.md の Topology で fan-out > 1 のノードを列挙し、各下流ブランチが最終出力で必要とする列セット (各 .tfl の Output 直前 RemoveColumns / Rename / 最終 Output schema から逆算) を抽出。

複数ブランチの列セットが交わらない、または特定の AddColumn 結果列を一部ブランチだけが必要とする場合、その AddColumn を分岐ノードと同じ .tfl に同居させてはいけない。**分岐ノードを最後とする intermediate** を 1 つ作り、各ブランチごとに別の intermediate を Input チェーンで継ぐ。

「1 entity 1 .tfl」原則 ([intermediate-decomposition.md](intermediate-decomposition.md)) はノード数視点で分割を抑える方向に働くが、本項目は列要件視点で分割を要求する方向。**両者が競合した場合は分割を優先** (parity 保全 > ノード数最小化)。

アンチパターン: 全ブランチに共通の Hyper を 1 つ出して全下流が共有する設計。

## 13. stg Transforms 表の op 値検証

`Materialization=live_pds` の stg entry の `Transforms (column-level)` 表で、`op` 値が `rename` / `cast` / `hide` の 3 値のいずれかになっているか。

augmenter ([prep-pds-augmenter](../../prep-pds-augmenter/SKILL.md)) で表現可能なのはこの 3 種のみ。Filter / AddColumn / ReplaceValue / TrimWhitespace / GroupValues 等の row-level 操作が Transforms 表に混じっていたら、当該 actions を stg ではなく intermediate 側に分割し直す ([../../../../references/layer-responsibilities.md](../../../../references/layer-responsibilities.md) の stg は column-level のみ)。

束縛層の制約も併せて確認する ([../../../../references/input-policy.md](../../../../references/input-policy.md) §stg を Live PDS で表現する場合): rename は vconn true rename で下流 Prep 消費が成立するが、**cast / hide の下流 Prep 生存は未検証**。live_pds stg に cast / hide を含める plan は Stop 2 で未検証リスクを明示する。

典型 anti-pattern: Input が vconn なので architect が「kind=vconn → augment → Materialization=live_pds」と早合点し、元 SuperTransform の actions 全部を Transforms 表に流し込んだ結果、Filter や AddColumn が混入する。

判定: Transforms 表の各行の op を集計し、`{rename, cast, hide}` 以外を 1 つでも検出したら是正する。是正方法は **Actions-level splits セクションで当該 actions を int_<stg と同名 entity>.tfl に分割** + stg Transforms 表からは削除。

prep-builder の build 時にも `verify` で同じチェックが走るため二重防御は効くが、decompose 段で潰すほうがやり直しが安い。

## 14. 是正と再出力

1〜13 で不整合や検討漏れが見つかったら、是正してから plan を出力する。
