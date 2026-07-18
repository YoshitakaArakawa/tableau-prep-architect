---
purpose: tableau-pds-augmenter が .tds XML を編集する際の構造仕様。Calculated Field の注入と column-level transforms (rename / hide / cast) の XML 形を定義
sources:
  - Sample - Superstore.tds (Tableau Desktop 同梱、Profit Ratio の構造)
  - https://help.tableau.com/current/api/rest_api/en-us/REST/rest_api_ref_data_sources.htm
fetched_at: 2026-05-19
note: extract-based (Hyper-backed) と live (virtual-connection backed) の両方の PDS で round-trip 成功を確認済
---

# TDS XML 編集フォーマット

## 目次

- Calc 注入 (calc-field)
  - .tds 内の位置
  - `<column>` 要素の属性
  - `<calculation>` 子要素 / XML escape
  - 注入の正解例
  - 検証ポイント (round-trip)
- Column-level Transforms (rename / hide / cast)
- .tds の 3 層構造 (なぜ cast は override では駄目か)
- 既知の制約

## .tds 内の位置

`<datasource>` 要素の直接子。**`</connection>` の外側**、`<aliases />` 直後、他 `<column>` 宣言の sibling として置く。

```xml
<datasource ...>
  <connection class='federated'>
    <named-connections>...</named-connections>
    <relation .../>
    <metadata-records>...</metadata-records>
  </connection>
  <aliases enabled='yes' />
  <!-- ↓ ここから calc field 群 -->
  <column caption='Profit Ratio' datatype='real' name='[Calculation_1368249927221915648]'
          role='measure' type='quantitative'>
    <calculation class='tableau' formula='SUM([Profit])/SUM([Sales])' />
  </column>
  <!-- ↑ ここまで -->
  <column datatype='string' name='[City]' .../>   <!-- 素のカラム宣言 -->
  <layout .../>
</datasource>
```

## `<column>` 要素の属性

| 属性 | 必須 | 値 |
|---|---|---|
| `caption` | yes | ユーザー可視ラベル (Tableau Desktop / Cloud の UI に表示) |
| `name` | yes | XML 内 ID。`[Calculation_<int>]` 形式が Tableau Desktop の慣例 |
| `datatype` | yes | `real` / `integer` / `string` / `boolean` / `date` / `datetime` |
| `role` | yes | `measure` / `dimension` |
| `type` | yes | `quantitative` (measure 既定) / `nominal` / `ordinal` |

`name` の opaque ID は **unix ms タイムスタンプの整数** (例: `[Calculation_1779186954563]`) を使う。Desktop が採番する形式と互換で、複数 calc を同時注入する場合は連番分散させると衝突しない。

## `<calculation>` 子要素

```xml
<calculation class='tableau' formula='<expr>' />
```

| 属性 | 値 |
|---|---|
| `class` | `tableau` (固定。bin / group / aggregation などには異なる値が入る) |
| `formula` | Tableau Calc 構文の式。XML attribute 内なので `<` `>` `&` `'` `"` の escape が必要 |

### XML escape

`formula` 内に特殊文字が含まれる場合の置換:

| 文字 | Escape |
|---|---|
| `&` | `&amp;` |
| `<` | `&lt;` |
| `>` | `&gt;` |
| `'` | `&apos;` |
| `"` | `&quot;` |

例: `IF [Status] = 'OK' THEN 1 ELSE 0 END` → `IF [Status] = &apos;OK&apos; THEN 1 ELSE 0 END`

日本語列名 (`[収支]` 等) は escape 不要。UTF-8 でそのまま書ける。

## 注入の正解例 (Profit Ratio)

```xml
<column caption='Profit Ratio' datatype='real' name='[Calculation_1368249927221915648]'
        role='measure' type='quantitative'>
  <calculation class='tableau' formula='SUM([Profit])/SUM([Sales])' />
</column>
```

## 検証ポイント (round-trip)

publish 後の再 DL で .tds 内に以下が残っていれば成功:

1. `name='[Calculation_<採番した int>]'` の `<column>` 要素
2. その属性 `caption` がスペック指定値と一致
3. その子 `<calculation>` の `formula` 属性に operands (列名・関数名) が残存

Cloud は publish 時に formula を字句的に書き換えない (本 Skill の検証で実証済み)。並べ替えもしないため、注入位置がそのまま保持される。

## Column-level Transforms (rename / hide / cast)

Skill が calc 注入と並んで扱う、既存 `<column>` 要素への属性編集。すべて `<datasource>` 直下の `<column ... />` (self-closing) 要素を対象にする。

### rename (caption-only): `caption` 属性の書き換え — extract / live source

```xml
<!-- before -->
<column caption='Workbook Repo Url' datatype='string' name='[bbbbbbbb-...]' role='dimension' type='nominal' />
<!-- after -->
<column caption='workbook_repo_url' datatype='string' name='[bbbbbbbb-...]' role='dimension' type='nominal' />
```

`name` 属性は不変。caption は BI / VizQL / Workbook の表示名にのみ効く。**下流 Prep (LoadSqlProxy) はフィールドを local-name (= `name` 属性) で束縛するため、caption-only rename は Prep からは見えない** (run 時 "Unknown field name")。

### rename (true rename): local-name 書き換え + `<cols>` map — vconn source

vconn source (新規合成 PDS で既存 consumer がいない) では rename を local-name 層まで書き換える。下流 Prep からも新名で読める:

```xml
<!-- 1. metadata-record: local-name を新名に (remote-name / remote-alias は物理名のまま) -->
<metadata-record class='column'>
  <remote-name>dddddddd-0000-0000-0000-000000000004</remote-name>
  <local-name>[ticker]</local-name>
  ...
</metadata-record>

<!-- 2. </metadata-records> 直後: 論理名 -> 物理列のマッピング (Desktop が論理名 != 物理名のとき出す機構) -->
<cols>
  <map key='[ticker]' value='[Transactions].[dddddddd-0000-0000-0000-000000000004]' />
</cols>

<!-- 3. <column>: name も caption も新名 -->
<column caption='ticker' datatype='string' name='[ticker]' role='dimension' type='nominal' />
```

既存 PDS (extract / live) への true rename は、その PDS を見ている workbook の field 参照を壊すため未サポート。Prep 消費前提の rename が既存 PDS に必要なら stg を実 .tfl で作る。

### hide: `hidden='true'` の追加

```xml
<column caption='Workbook Repo Url' datatype='string' name='[bbbbbbbb-...]' role='dimension' type='nominal' hidden='true' />
```

`hidden='true'` を持つ `<column>` は VizQL Metadata API の field 一覧から除外される。Workbook / Prep input の picker からも見えなくなる。

### cast: hidden + cast calc の組合せ

`<column datatype>` の書き換え単独では VizQL 層に届かない (cosmetic) ため、cast は 2 ステップで実現:

1. 元 column に `hidden='true'` を追加 (上記 hide と同じ)
2. 新規 calc column を追加 (下記 Calculated Field と同じ形)、formula は datatype 別の cast 関数:

```xml
<column caption='view_count' datatype='real' name='[Calculation_<unix-ms>]' role='measure' type='quantitative'>
  <calculation class='tableau' formula='FLOAT([cccccccc-...])' />
</column>
```

cast 関数の対応:

| to_datatype | default formula |
|---|---|
| `real` | `FLOAT(<orig>)` |
| `integer` | `INT(<orig>)` |
| `string` | `STR(<orig>)` |
| `date` | `DATE(<orig>)` |
| `datetime` | `DATETIME(<orig>)` |
| `boolean` | default なし (caller が cast_formula を明示) |

新規 calc の `<column>` は元 column と sibling 関係で、`<aliases enabled='yes' />` 直後 (calc 注入と同じ位置) に挿入する。

## .tds の 3 層構造 (なぜ cast は override では駄目か)

.tds の column 表現は層をなしている:

| 層 | 用途 | 編集が反映される consumer |
|---|---|---|
| `<metadata-records><local-type>` 等 | vconn / extract が報告する source-of-truth | 編集してもサーバーで上書きされ、PDS 側からは触れない扱い |
| `<column datatype>` (`<datasource>` 直下) | Desktop UI 表示の override | **Desktop UI のみ**。VizQL Metadata API / query 層には届かない |
| `<column name>` / `<local-name>` (+ `<cols><map>`) | フィールドの論理 ID (束縛層) | **下流 Prep (LoadSqlProxy) はここで束縛する**。true rename が書き換える層 |
| `<column caption>` | 表示名 | **BI / VizQL / Workbook のみ**。下流 Prep には効かない |
| `<column>` 直下の `<calculation>` 子要素 | 新規 calc field | **全層 (新規 field として exposure される)** |

caption 層と local-name 層の分離が rename の 2 semantics (caption-only / true rename) の根拠。hide は hidden 属性層で BI / VizQL に効く (下流 Prep への遮蔽は未検証)。型変更は datatype 層では cosmetic にとどまるので、cast op では calc 層 (新規 field) を使って実体ある型変換を行う。

## 既知の制約

- 既存 calc field の **編集・削除** は本フォーマット仕様の範囲外 (本 Skill のスコープも注入のみ)
- 既存 column の **削除** は scope 外 (vconn / extract schema との整合崩壊リスク)。hide で suppress に留める
- 計算式の **構文検証** はサーバー publish 試行のみ (ローカル lint は持たない)。構文エラーは publish 時 HTTP 400 で発覚
- VizQL 層で cast が effective かの最終 assertion は本 Skill 外。caller が `mcp__tableau__get-datasource-metadata` で `dataType: REAL` / `columnClass: CALCULATION` を確認する
