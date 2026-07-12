---
purpose: decomposition-plan 出力直前に必ず通す 17 項目 self-check の判定基準集 (正典)
note: 各項目は「何を確認するか」と「判定方法・是正手順」に絞る。規範の背景 (Rename-back の原理、Input dispatch、Union 保持理由等) は各正典ファイルに委譲し、本ファイルでは繰り返さない
---

# decompose 完了前 self-check (詳細版)

plan.json を書き終えて `render_plan_md.py` を流す前に **必ず通す** 17 項目チェックの正典。

## 目次

- 1-4: lineage / Union 保持 / Output mapping (構造の整合)
- 5-8: staging 集中 / Input 集約 / DAG / Joins (書式と配置)
- 9-12: 上流移行検討 / 列削除順序 / 分岐列要件 (最適化と保全)
- 13-16: live_pds op 検証 / Rename-back / Input 出所 / incremental (条件付き項目)
- 17: 是正と再出力

prep-builder の build 開始時にも `verify_lineage_closure` / `verify_edge_namespaces` で機械的に二重防御するが、decompose 段で潰しておくほうがやり直しが安い (build → publish → run fail → rebuild の loop が 10-15 min)。

**操作のシリアライズ形式は本チェックに影響しない**: flow-summary.md の actions inventory は flat SuperTransform / Container (` [container 形式]`) / Input renames (` [Input renames]`) の 3 形式を同じ形で収録する ([flow-summary-format.md](../../prep-extractor/references/flow-summary-format.md#supertransform-actions-inventory))。以下の全項目は操作の **type と topological 位置** で判定するので、どの形式でも扱いは同一。難読化フロー (列名が UUID) では表示名 (日本語含む) が Input renames と各 field caption に現れる — stg 境界の rename 翻訳や列削除順序 (項目 11) を読むときはこの層を参照する。build 時は `normalize_source_containers` が Container を flat 化するので actions 分割も形式非依存。

## 1. Upstream lineage (機械検証)

Upstream lineage 表は `render_plan_md.py` が plan.json から計算してレンダリングし、宣言 Input から到達できない step は render がエラーで止まる ([decomposition-plan-format.md](../../../../references/decomposition-plan-format.md) の Lineage closure invariant 節)。本チェックでの確認は「render がエラーなしで通ったか」で足りる。

## 2. Prev 連鎖の到達確認 (配置の設計妥当性)

render の lineage 検証は「到達可能か」しか見ない。**その配置が業務的に正しい branch か** は本チェックの責務: 各 entry の `inputs[].replaces_steps` が「元フローでその step 群を実際に feed していた直接の親」を指しているか、flow-summary の Topology 表で確認する。**下流の結合キーから逆推定しない** (列が上流 Input に存在せず run 時 `Unknown field name` で失敗)。

## 3. SuperUnion 削除禁止

削除提案ノード一覧に SuperUnion を含めない。Union は actions=0 / 入力ブランチが同一に見えても削除候補にしない。理由 (`Table Names` 暗黙注入列への下流依存) と判断手順は [intermediate-decomposition.md §Union ノードは削除候補にしない](intermediate-decomposition.md#union-ノードは削除候補にしない-hard-rule)。Union 周辺の no-op を畳む提案は、Union 出力スキーマの下流参照を全洗いしてからにする。

## 4. Output mapping (source_original_output_name)

元フローの全 output PDS それぞれに、それを引き継ぐ flow の `source_original_output_name` が plan.json 上で割り当てられているか (存在しない output 名は構造検証が弾くが、**割り当て漏れ = null のまま** は機械検証されない)。漏れると prep-output-comparator がペアを組めない ([decomposition-plan-format.md](../../../../references/decomposition-plan-format.md) の Output mapping 節)。

## 5. 型変換 / 名前変換を staging に集中

intermediate / marts レイヤの `Included original steps` に `ChangeType` (型キャスト) や `Rename` (列名変更) を含む actions が残っていたら、staging の責務漏れの疑い。`Actions-level splits` セクションで該当 actions を stg 側に巻き戻すことを検討する。

例外: intermediate での Join 後にしか型が確定しない列 (`derived = TOFLOAT(col_a + col_b)` 等) は intermediate に残して良い。

## 6. 同一ソースの Input 集約

複数の stg .tfl が同一の `Source` (例: `vc_salesforce / Opportunity`) を Input にしていたら統合候補。

判定: `## New .tfl files` の各 stg セクションの `Inputs` を集計し、同じ Source 名が複数 .tfl に出現していないか確認。

例外: 同一テーブルから **異なる列セット** を取り出して別ドメインに供給するケース (`stg_orders__metrics` と `stg_orders__metadata` が同じ `Orders` テーブルから別目的で columns を抜く) は分離保持で良い。

## 7. Dependency DAG Before/After

`## Dependency DAG (Mermaid)` に Before / After 2 ブロックが揃っているか ([decomposition-plan-format.md](../../../../references/decomposition-plan-format.md) の Dependency DAG 節)。

## 8. Joins cardinality

Join を含む .tfl に `**Joins**` フィールドが書かれ、cardinality (1:1 / 1:N / N:1 / N:N / 不明) が記載されているか。SuperJoin ノード、または .tfl 内で Join を行うステップを含む .tfl で必須。**不明な場合も `不明` と明示** (空欄不可、書式は [decomposition-plan-format.md](../../../../references/decomposition-plan-format.md) の Joins field の書式 節)。

## 9. marts AddCol の上流移行検討

marts レイヤに残っている `AddCol` (計算フィールド追加) actions を上流 (intermediate) で実施できないか検討したか。

検討の結果 marts 残置が妥当な場合はそれで良い (例: BI 表示用整形、行単位の派生列でかつ他フローから再利用しないもの)。判定は user 側に判断材料を提示する形で良い。

## 10. marts Filter の上流移行検討

marts レイヤに残っている Filter actions を上流 (intermediate / staging) で実施して行数削減を前倒しできないか検討したか。

検討の結果 marts 残置が妥当な場合はそれで良い (例: marts 固有のサンプリング・上位 N 件、後段で full history が必要)。

## 11. 列削除 actions の元順序保全

元 .tfl の各 SuperTransform 内で「列を削除する action (RemoveColumns、または column を消す副作用を持つ他 action) と、その列を参照する他 actions」の前後関係を、分解後の .tfl 間 / .tfl 内で逆転させていないこと。

stg / int に actions を分割するとき、列削除 action を上流 .tfl に置いたら、その削除列を参照する下流 actions が同じ .tfl 内かさらに上流に到達できなければならない。参照とは FIXED LOD の partition key / ORDERBY / AddColumn 式の参照列 / Filter 式の参照列 / Join clause の結合キー のすべて。

判定方法: 各列の最終削除 step を topological 上で特定し、その列名を文字列マッチで参照する全 step が削除 step より「上流」にあるか確認。逆転していたら、列削除 action を下流 .tfl 側に移すか、分割境界をずらす。

例外なし (`Table Names` 等の Union 暗黙注入列も同じ精神で扱う、項目 3 と独立に判定)。

## 12. 分岐ノード下流の列要件チェック

flow-summary.md の Topology で fan-out > 1 のノードを列挙し、各下流ブランチが最終出力で必要とする列セット (各 .tfl の Output 直前 RemoveColumns / Rename / 最終 Output schema から逆算) を抽出。

複数ブランチの列セットが交わらない、または特定の AddColumn 結果列を一部ブランチだけが必要とする場合、その AddColumn を分岐ノードと同じ .tfl に同居させてはいけない。**分岐ノードを最後とする intermediate** を 1 つ作り、各ブランチごとに別の intermediate を Input チェーンで継ぐ。

「1 entity 1 .tfl」原則 ([intermediate-decomposition.md](intermediate-decomposition.md)) はノード数視点で分割を抑える方向に働くが、本項目は列要件視点で分割を要求する方向。**両者が競合した場合は分割を優先** (parity 保全 > ノード数最小化)。

アンチパターン: 全ブランチに共通の Hyper を 1 つ出して全下流が共有する設計。

## 13. stg Transforms 表の op 値検証

`Materialization=live_pds` の stg entry の `Transforms (column-level)` 表で、`op` 値が `rename` / `cast` / `hide` の 3 値のいずれかになっているか (制約の根拠は [layer-responsibilities.md](../../../../references/layer-responsibilities.md) §staging)。

判定: Transforms 表の各行の op を集計し、`{rename, cast, hide}` 以外 (Filter / AddColumn / ReplaceValue / TrimWhitespace / GroupValues 等の row-level 操作) を 1 つでも検出したら是正する。是正方法は **Actions-level splits セクションで当該 actions を int_<stg と同名 entity>.tfl に分割** + stg Transforms 表からは削除。

束縛層の制約も併せて確認する ([input-policy.md](../../../../references/input-policy.md) §stg を Live PDS で表現する場合): **cast / hide の下流 Prep 生存は未検証** のため、live_pds stg に cast / hide を含める plan は Stop 2 で未検証リスクを明示する。

## 14. mart Rename-back 表のカバレッジ (内部名の露出ゼロ)

`## Output mapping` に行を持つ mart (= 元 output を引き継ぐ mart) は **Rename-back 表** を必須で持ち、その mart に到達する rename 済み列を全カバーしているか (原理と書式は [decomposition-plan-format.md §Rename-back](../../../../references/decomposition-plan-format.md))。

判定手順:

1. stg 翻訳 + 翻訳済み派生列 (サフィックス付き変種を含む) から、その mart の出力に到達する列を列挙する
2. 各列が Rename-back 表に「internal name → original name」の行を持つか照合する
3. 漏れ = 内部名が既存消費者向け出力に露出する。サフィックス保存の適用漏れが典型的な取りこぼし。逆に **rename を経ていない列を表に載せない** (無意味な RenameColumn action が増える)

新規 mart (Output mapping に行が無い) は本項目の対象外。

## 15. Input 出所分類と stg 再利用

**(a) passthrough 入力の出所分類**: pds 入力ごとに「その PDS が in-scope の別フローの出力か、外部 raw か」を判定し、plan の当該 Input 行に明記する。

- **in-scope フローの出力** → passthrough は**暫定**。plan に「上流 (フロー名) の移行完了後に新 PDS へ差し替え」の追跡メモを Output mapping 近くに残す。移行順は producer 先行が原則 (依存の実抽出は prep-extractor Phase C の `flow-dependencies.md`。無ければ deploy-context の出所プロジェクトと PDS 命名から判断し、`未確認` とラベルする)
- **外部 raw** → passthrough は恒久。差し替え追跡不要

判定を省くと「暫定のつもりが恒久扱い」で旧資産アーカイブ時に下流が壊れる。

**(b) stg 再利用チェック**: vconn 入力の (vconn_luid, table_uuid) が、既存の稼働中 stg (deploy-context の target 配下 stg datasources + 先行セッションの plan/manifest) と同一テーブルを指すなら、**新規 stg を作らず既存 stg PDS を Input 再利用する案をデフォルト**にして Stop 2 に出す (dbt の単一 stg 原則)。列カバレッジ (この flow が必要とする列が既存 stg の公開列に全部あるか) を確認し、不足列があれば既存 stg の拡張 vs 新規作成を比較提示する。

同一 vconn テーブルから stg が 2 本できるのは、命名も rename 翻訳も分裂する二重管理の始まり。既存 stg の rename 済み列名に下流の式翻訳を合わせること。

## 16. Incremental / append 出力の継承方針

flow-summary.md の Meta に `Incremental inputs` / `Append-mode outputs` 行がある (または Warnings に 🔒 Incremental/append flow がある) 場合、plan に以下を含める:

- **継承方針の明示**: 元 Output の append モード + Input の incremental refresh 設定を分解後のどの .tfl が引き継ぐか (通常は元 Output を引き継ぐ mart)。引き継がない場合はその根拠と、full-refresh 化による影響 (履歴の非蓄積)
- **履歴 backfill の要否**: 元 output PDS は過去 run の累積で、現在のソースには残っていない過去バッチを含みうる。新 mart にその履歴が必要か (必要なら旧 PDS からの initial load 案を提示)
- **parity 検証方法の切り替え**: 全体行数一致は原理的に成立しない。Output mapping 近くに「compare は control field (例: `Date`) の期間一致で行う」と明記し、control field 名を plan に書く (comparator への引き渡し情報)

これらは業務判断を含むため **Stop 2 の Tier 1 に必ず載せる**。省くと compare が false FAIL を出し、原因調査の手戻りになる。

## 17. 是正と再出力

1〜16 で不整合や検討漏れが見つかったら、是正してから plan を出力する。
