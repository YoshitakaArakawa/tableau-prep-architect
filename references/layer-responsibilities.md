---
purpose: dbt 流 staging / intermediate / marts 各レイヤの責務定義と判定基準
fetched_at: 2026-05-17
note: 各レイヤの「やる/やらない」、レイヤ判定の決定木、actions レベル分析の指針を含む
---

# layer-responsibilities

各レイヤの責務を詳細に定義する。「このステップ群はどのレイヤに属するか」の判断基準を提供。**decompose** で中核的に参照、**analyze**（レイヤ推定時）と **build**（配置先決定時）でも参照される。

## 各レイヤの責務

### staging (`stg_*`)

| やる | やらない |
|---|---|
| 1 ソースに対する型キャスト | JOIN（同ソース内の self-join も避ける） |
| 列リネーム | ビジネスロジック・計算ルール |
| 最低限のクレンジング（NULL 処理、明らかな欠損補完） | 集約 |
| Tableau の Input ノード（**仮想接続 / Published DS** が前提、[input-policy](input-policy.md)） | 他レイヤへの直接依存 |
| 中間 Hyper への出力 or stg_published Data Source 化 | — |

**目安**: 1 stg .tfl は **ノード 5〜15 個**。これを超えるなら staging の責務を逸脱している。

### intermediate (`int_*`)

| やる | やらない |
|---|---|
| staging 同士の JOIN | 1 ソースだけの整形（staging の責務） |
| ビジネスロジック（売上区分、有効/無効判定、フラグ生成） | 最終出力（外部 publish しない、Hyper 中間が基本） |
| 前処理集約（marts より細かい粒度の集約も含む） | fct × dim の最終結合（marts では別 .tfl / 別 Published DS に保つ） |
| ピボット / アンピボット | — |
| Python / R ステップ | — |

**目安**: intermediate は **原則 1 entity 1 .tfl**（30+ ノードまで許容）。連鎖分割は例外扱い — 詳細は [intermediate-decomposition.md](intermediate-decomposition.md) を参照。

### marts (`fct_*` / `dim_*` / `rpt_*`)

mart 層は次の三本立て + 事前集計派生で構成:

| ファイル種別 | 役割 |
|---|---|
| `fct_<entity>.tfl` | 1 ファクト 1 ファイル。Published DS として publish。再利用可能な「素材」 |
| `dim_<entity>.tfl` | 1 ディメンション 1 ファイル。Published DS として publish。複数 fct で共有 |
| `rpt_<scope>.tfl` | fct × dim を **Prep 内で物理 JOIN 済み** の OBT。BI が複数 dim 込みで読む単位 |
| `agg_<entity>_<grain>.tfl` | fct から派生する事前集計 OBT。粒度を名前に明示。**atomic な fct から再計算可能** であること |

| やる | やらない |
|---|---|
| 最終ファクト / ディメンションの形成 | 生データへの直接依存 |
| `fct_` と `dim_` を **別 .tfl・別 Published DS** として publish | intermediate ロジックの混入 |
| BI が複数 dim を組合せて読む用途には `rpt_*.tfl` を作って結合済み Published DS を提供 | — |

**なぜ rpt_ を作るのか**: Tableau Workbook の Data Model では **Published DS 同士を Relationship / Join できない**。Workbook 側で組合せる手段は Data Blending のみで、非加法集計（COUNTD / MEDIAN 等）や複雑なジョイン条件には制約がある。そのため複数 dim を結合した分析が必要なら **Prep 内で物理 JOIN した OBT を別 Published DS として持つ** のが現実解。

**目安**: 1 mart .tfl は **ノード 5〜15 個**。intermediate ですべて済ませてから、marts は最終形に整えるだけのはず。

## レイヤ境界の判定基準

「あるステップがどのレイヤに属するか」を判断するチェックリスト：

| シグナル | 判断 |
|---|---|
| 1 ソースの直接整形のみ | → staging |
| 異なるソースを JOIN している | → intermediate |
| 粒度が変わる集約をしている（行レベル → 顧客レベル等） | → intermediate（marts ではない） |
| ビジネスロジック（売上区分・有効フラグ生成等） | → intermediate |
| 出力ノードが Published DS で、後段の BI が直接使う | → marts |
| 単独の fact / dim として再利用したい | → marts（fct or dim） |
| 複数 dim を結合済みで BI に提供したい | → marts（rpt） |
| 重い集計を BI で繰り返し使うため事前マテリアライズしたい | → marts（agg、fct から派生） |

迷ったら **より下流のレイヤに置く方が安全**（後で staging に戻すのは簡単、逆は難しい）。

## 1 .tfl 1 主要変換の原則

dbt の「1 モデル 1 SELECT」を Prep に転用したもの：

- 1 .tfl は **1 つの主要変換** を担う
- 「1 つの主要変換」とは: 1 つの最終出力ノードに向かう、論理的にまとまったステップ群
- 複数の Output ノードを持つ .tfl は **分割すべきサイン**

例：
- ✅ `int_orders_enriched.tfl` — stg_orders ＋ stg_customers ＋ stg_products を JOIN して 1 つの enriched テーブルを出力
- ❌ `int_everything.tfl` — 上記に加え、別系統の集約も同梱、複数 Output で 4 つのテーブルを出す

## intermediate 分解戦略

連鎖分割パターン・分割の目安・actions 単位分割の判断基準は [intermediate-decomposition.md](intermediate-decomposition.md) を参照。本ファイルは「どのレイヤに属するか」、intermediate-decomposition.md は「intermediate 内をどう分けるか」の責務分担。

## ⭐ SuperTransform の actions レベル分析

**重要観点**: 長大フローの分解で、**SuperTransform ノードの中身（`actions` 配列）を読まないとレイヤ帰属が決まらない**。

Clean ステップ 1 つが：
- 「リネームのみ」なら → **stg**
- 「ビジネスロジック計算列追加」なら → **int**
- 「最終整形（UI 向けラベル付け）」なら → **mart**

つまり、**ノード単位のレイヤ判定は近似値**、**actions 単位の判定が真の判断**。

### actions レベル判定の早見

詳細は [prep-ui-to-json-mapping.md](prep-ui-to-json-mapping.md) の actions サブタイプ表を参照。要約:

| actions の内容 | 推奨レイヤ |
|---|---|
| Rename + ChangeColumnType + 簡単な RemoveColumns | **stg** |
| TrimWhitespace + FixCase + 単純 ReplaceValue | **stg** |
| 軽いフィルタ（IS NOT NULL 等） | **stg** |
| 計算列（純粋関数・業務ルール）の AddColumn | **int** |
| GroupValues（ビジネスロジック由来） | **int** |
| 複雑な ReplaceValue（業務マッピング） | **int** |
| UI 向け列リネーム・最終並べ替え | **mart** |

→ **1 つの SuperTransform に複数レイヤに跨る actions が混在するケースが頻繁** にある（stg 相当の Rename と int 相当の AddColumn が同居）。

### decompose 設計時に判断すべきこと

actions レベル分析の結果、decompose は以下を **設計案として明示する**:

- **SuperTransform をレイヤ境界で分割するか**: 例「Clean 5 を 2 つに分割し、Rename×3 を stg_snowflake__orders へ、AddColumn×2 + Filter を int_orders_enriched へ」
- **actions 順序を変えるべきか**: 例「Filter を Rename より前に出して後段の処理量を削減」（順序変更が結果に影響する不安があれば **順序を保つ方が安全**）
- **レイヤを跨いで紛れているステップを再配置するか**: 例「int の中に紛れた Rename だけの SuperTransform を stg に戻す」

これらは decompose の出力（[decomposition-plan-format.md](decomposition-plan-format.md) の `Actions-level splits` セクション）に **ユーザーが確認できる粒度で書き出す**。実装は prep-builder が設計案に厳密に従って行う。

⚠️ **自律的な並び替え・再配置はしない**:
- actions 種別の判定ミス（リネームのつもりが破壊的操作）
- 元の意図不明な処理を勝手に再配置
- 意図しないノード並び替えが結果を変える可能性

迷ったら設計案に書かない（= 元の構造を保つ）方を選ぶ。

## fct / dim / rpt / agg の役割分担

```
intermediate
├── int_orders_enriched.tfl
└── int_customer_dimensions.tfl
        ↓                ↓
marts
├── fct_sales.tfl                  → Published DS: fct_sales        (再利用素材)
├── dim_customer.tfl               → Published DS: dim_customer     (再利用素材)
├── rpt_sales_with_customer.tfl    → Published DS: rpt_sales_with_customer
│                                     (fct_sales × dim_customer を Prep 内で JOIN 済み OBT)
│                                     (BI Workbook はこれを単一 DS として読む)
└── agg_revenue_monthly.tfl        → Published DS: agg_revenue_monthly
                                     (fct_sales を月次粒度で事前集計、BI で繰り返し使う)
```

設計の意図:

- **fct_ / dim_** は **再利用可能な素材**。`dim_customer` を `fct_sales` / `fct_orders` / `fct_returns` から共有できる
- **rpt_** は **BI 用の完成品**。Workbook では Published DS 同士の Relationship/Join が使えないため、複数 dim と結合した状態が必要な分析タスクごとに rpt を物理化する
- **agg_** は **事前集計の物理化**。同じ重い集計（月次売上 / 顧客別 LTV 等）を BI で何度もやる場合、**fct から派生** させて agg として publish する。粒度は名前に明示（`agg_revenue_monthly` 等）、agg は raw source から独立に組まず、**atomic な fct から再計算可能** に保つ
- **マテリアライズの粒度を分析タスク単位に揃える**: 必要な rpt / agg だけ作るので、巨大な汎用 OBT を作って全分析を 1 つの DS に押し込む形は避ける

軽い分析（メトリック 1〜2 個を別 dim から重ねるだけ等）は fct_/dim_ 直読 + Data Blending で済ませてよい。rpt_ は「Data Blending では足りない / 複数 dim の組合せ的分析」の場合に作る。agg_ は「同じ重い集計を何度も BI でする」場合に作る。

## アンチパターン

### staging に JOIN を入れる

```
❌ stg_orders_with_customers.tfl  (stg なのに JOIN がある)
✅ stg_orders.tfl + stg_customers.tfl + int_orders_with_customers.tfl
```

### marts でビジネスロジックを書く

```
❌ fct_sales.tfl 内で「優良顧客フラグ」を計算
✅ int_customer_classified.tfl で計算 → marts は集約のみ
```

### 巨大 intermediate を放置

```
❌ int_everything.tfl (60 ノード、複数 entity の変換が一緒くた)
✅ entity 別に分割: int_orders_*.tfl + int_customers_*.tfl + int_products_*.tfl
```

intermediate は **原則 1 entity 1 .tfl**（[intermediate-decomposition.md](intermediate-decomposition.md)）。「巨大」の解は entity 分割が一般解。同一 entity 内が 30+ ノードに膨らむ場合のみ、同ファイルの例外条件（中間結果を別 entity からも参照する等）を満たすことを確認した上で `int_<entity>_step1_*` の連鎖分割に進む。

### fct_ の中に dim 情報を埋め込む

```
❌ fct_sales.tfl の中で customer 情報を JOIN 済みにして 1 つの Published DS にする
   （fct と dim の境界が壊れ、dim_customer を別 fct から再利用できなくなる）
✅ fct_sales.tfl + dim_customer.tfl は単体 Published DS として publish。
   結合済みが必要な分析タスクには rpt_sales_with_customer.tfl を別途作る
```

### 汎用 OBT を 1 つだけ作って全分析を押し込む

```
❌ rpt_all_sales_data.tfl（全 dim を結合した巨大 OBT を 1 つだけ作り、全分析がこれを読む）
✅ 分析タスクごとに必要な dim だけ JOIN した rpt_<scope>.tfl を作る
   （無駄な列・行を持たない、マテリアライズコストも分散）
```

### agg を raw source / int から独立に組む

```
❌ agg_revenue_monthly.tfl が stg_orders や int_orders_enriched を Input にして月次集計
   （fct_sales の集計ロジックと重複し、定義の乖離リスク）
✅ agg_revenue_monthly.tfl は fct_sales を Input にして粒度変更のみ
   （ビジネスロジックは fct で一意、agg は再集計だけを担う）
```

## 参考

- 命名規約: [naming-conventions.md](naming-conventions.md)
- intermediate 分解戦略: [../.claude/skills/prep-architect/references/intermediate-decomposition.md](../.claude/skills/prep-architect/references/intermediate-decomposition.md)
- Input ポリシー: [input-policy.md](input-policy.md)
