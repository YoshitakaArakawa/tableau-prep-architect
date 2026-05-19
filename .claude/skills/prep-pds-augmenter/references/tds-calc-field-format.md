---
purpose: Tableau Calculated Field を .tds XML に注入する際の構造仕様。prep-pds-augmenter が参照
sources:
  - Sample - Superstore.tds (Tableau Desktop 同梱、Profit Ratio の構造)
  - https://help.tableau.com/current/api/rest_api/en-us/REST/rest_api_ref_data_sources.htm
fetched_at: 2026-05-19
note: .tds (XML) 内の Calculated Field 表現方法と、prep-pds-augmenter が採用する注入ルールを定義。Hyper extract-based PDS で検証済 (round-trip 成功)
---

# TDS Calculated Field 注入フォーマット

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

## 既知の制約

- live-connection (extract 非 hyper) PDS への注入は未検証
- 既存 calc field の **編集・削除** は本フォーマット仕様の範囲外 (本 Skill のスコープも注入のみ)
- 計算式の **構文検証** はサーバー publish 試行のみ (ローカル lint は持たない)。構文エラーは publish 時 HTTP 400 で発覚
