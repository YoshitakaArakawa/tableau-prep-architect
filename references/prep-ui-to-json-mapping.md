---
purpose: Tableau Prep の UI ステップ名と flow.json 内 nodeType の対応表
sources:
  - https://help.tableau.com/current/prep/en-us/
fetched_at: 2026-05-17
source_last_known_update: 不明（公式網羅 docs が無いため実例ベース）
note: UI Step ⇔ nodeType ⇔ actions サブタイプの対応のみを扱う構造定義。レイヤ示唆などの判断基準は含まない
---

# prep-ui-to-json-mapping

Tableau Prep の **UI ステップ名** と **flow.json 内 `nodeType`** の対応表。**構造定義のみ**。レイヤ示唆などの判断基準は含まない（それは prep-architect の責務）。

prep-extractor が flow.json を読むときの解釈、prep-builder が新規ノードを組み立てるときの根拠に使う。

⚠️ Tableau は flow.json の公式網羅ドキュメントを公開していないため、実例ベースで埋めている。本表に無い nodeType に遭遇したら追記する。

## UI Step ⇔ nodeType マッピング

| UI ステップ | 内部 nodeType | 備考 |
|---|---|---|
| Input - SQL / Custom SQL | `LoadSql` | 通常の SQL ベース Input。connection が `sqlproxy` なら仮想接続経由の可能性大 |
| Input - Published Data Source | `LoadSqlProxy` | Tableau Server プロキシ経由 |
| Input - CSV | `LoadCsv` | ファイル入力 |
| Input - Excel | `LoadExcel` | ファイル入力 |
| Input - Hyper | `LoadHyper` | 中間 .hyper ファイル入力 |
| Clean ステップ | `SuperTransform` | 複数 actions を内包する万能ステップ（下記参照） |
| Join ステップ | `SuperJoin` | 結合 |
| Union ステップ | `SuperUnion` | UNION |
| Aggregate ステップ | `SuperAggregate` | 集約 |
| Pivot ステップ | `SuperPivot`（バージョン依存） | 縦横ピボット |
| New Rows ステップ | `SuperNewRows` | 時系列補間、連番生成、null 行追加 |
| Python / R ステップ | `Script` 系（要サンプル確認） | 外部スクリプト |
| Output - Hyper（ローカル/ファイル） | `WriteToHyper` | .hyper 書き出し |
| Output - Published Data Source | `PublishExtract` | Tableau Server へ publish |
| Output - Database | `WriteToDatabase` | DB テーブル書き出し |

## バージョンプレフィクス

nodeType は `.v<year>_<minor>_<patch>.<Type>` 形式。

例:
- `.v2018_2_3.SuperTransform`
- `.v2019_3_1.LoadSqlProxy`
- `.v2021_3_1.SuperNewRows`

同じ論理ステップでもバージョン違いの internal type が同一フロー内に混在することがある（フロー作成・編集された Tableau Prep のバージョンで決まる）。グルーピング時は最後のドット以降を使う。

## SuperTransform の actions サブタイプ

1 つの SuperTransform ノードに **複数の actions** が並ぶ。各 action が UI 上の個別操作に対応:

| action type（推定） | UI 操作 |
|---|---|
| `RenameColumn` | 列リネーム |
| `ChangeColumnType` | 型キャスト |
| `RemoveColumns` | 列削除 |
| `ValueFilter` | 値フィルタ（IS NOT NULL, = 'x' 等） |
| `FilterOperation` | 条件フィルタ |
| `AddColumn` | 計算列追加（IF/CASE/CONCAT/LOD 等） |
| `GroupValues` | 値のグループ化（"USA"/"U.S.A." → "US"） |
| `Split` | 列分割（氏名 → 姓・名） |
| `TrimWhitespace` | 前後空白除去 |
| `FixCase` | 大文字小文字統一 |
| `ReplaceValue` | 値置換 |

⚠️ action type の正確な命名は要検証（実例ベースの推定）。本表に無い action type に遭遇したら追記する。

actions の構造詳細は [tfl-json-schema.md](tfl-json-schema.md#supertransform-内部の-actions) 参照。

## 未確認項目

実例で未観測のため要検証:

- Pivot ステップの正確な internal type
- Custom SQL Input の細部
- 仮想接続経由 Input の typeRef
- Python / R ステップの actual type
- Filter / AddColumn の actions 構造の詳細フィールド

該当ケースに遭遇したサンプルを extractor にかけて本表へ追記する。
