---
name: prep-pds-augmenter
description: Tableau Cloud / Server 上の Published Data Source を Calculated Field 注入と column-level transforms (rename / cast / hide) で機械的に改変・量産する Skill。extract-based (Hyper-backed) と live-connection (virtual-connection backed) の両方をサポート。download → .tds XML 編集 → publish (CreateNew default / Overwrite) → 再 DL 検証を一気通貫で実行。Prep フローが publish した Hyper Output に派生列を足したいとき、stg レイヤを Prep の代わりに Live PDS で表現したいとき (rename / cast / hide で stg 責務を XML 編集に置き換える)、calc 込み PDS を量産したいときに起動。caller が calc 仕様 (caption / formula / datatype) や transform 仕様 (column_name / to_caption / to_datatype) を明示提供する前提で、formula や naming 規約 (snake_case 化等) の auto-detect はしない。
---

# prep-pds-augmenter

Tableau Cloud / Server 上の Published Data Source (PDS) を **transforms** (rename / cast / hide) と **calc 注入** で機械的に改変・量産する Skill。extract-based (Hyper) と live-connection (virtual connection backed) の両方を扱える。

主な用途:
- Prep flow が出力した Hyper PDS に汎用的な派生列 (利益率 / 換算金額 / 閾値フラグ) を後付けで足す (= 既存ユースケース)
- 仮想接続経由の Live PDS に対して stg レイヤ責務 (型キャスト / リネーム / 不要列の hide) を Prep の代わりに XML 編集で表現する (= 拡張ユースケース、物理化を避けたいとき)
- composable PDS 公開時に派生列込みの PDS を量産する編集パイプライン

書き込み副作用 (新規 PDS 作成 or 既存 PDS 上書き) を伴うため、caller が target / new name / transforms / calcs を明示合意してから呼ぶ前提。

## スコープ

含む:
- extract-based (Hyper-backed) PDS への calc field 注入 (`source.kind: "extract"`、default)
- live-connection PDS への calc field 注入 + transforms (`source.kind: "live"`)
- transforms 操作: **rename** (caption 書き換え) / **cast** (hidden + cast calc) / **hide** (`hidden='true'`)
- CreateNew で別 PDS として publish (default、安全)
- Overwrite で同名 PDS を XML 差分のみで置換 (明示指定時のみ)
- 編集後の round-trip 検証 (再 DL して transform / calc が残ったか確認)

含まない:
- 既存 calc field の **編集・削除** — 注入のみ (将来拡張で考慮)
- 既存 column の **削除** — vconn / extract schema との整合を崩すリスクが高いので hide で suppress に留める
- formula の auto-detect / 推論 — caller 提供必須 (沈黙 fallback 回避)
- caption の naming 規約自動変換 (snake_case 化等) — caller が `to_caption` を 1 列ずつ明示提供。caller 側で雛形を生成するヘルパーは別 script で提供する余地あり
- .hyper のデータ本体の変更 — XML 編集のみで派生列・型変更を表現
- VizQL Metadata API での型 assertion — Skill 内 verify は .tds XML round-trip まで。VizQL 層の最終確認 (calc が `dataType: REAL` / `columnClass: CALCULATION` で見えているか等) は caller が `mcp__tableau__get-datasource-metadata` で別途行う前提 (AVG/SUM 値による型推定は Tableau の auto-promotion で判別不能なので使わない)

## 動作モデル

1 サイクル = (source PDS 1 個) + (transforms M 個) + (calc spec N 個) → (target PDS 1 個 publish)。
transforms と calcs はどちらか一方でも、両方でも良い (両方空は spec validation error)。

### 入力

`spec.json` 形式で渡す。例 (Live PDS に対する stg 用 transforms + ad-hoc calc):

```json
{
  "source": { "kind": "live", "luid": "<src-pds-luid>" },
  "target": { "project_id": "<target-project-luid>", "new_name": "stg_vconn__tableau_public" },
  "mode": "CreateNew",
  "transforms": [
    { "op": "rename", "column_name": "[<uuid>]", "to_caption": "workbook_repo_url" },
    { "op": "cast",   "column_name": "[<uuid>]", "to_caption": "view_count", "to_datatype": "real" },
    { "op": "hide",   "column_name": "[<uuid>]" }
  ],
  "calcs": [
    {
      "caption": "Profit Ratio",
      "formula": "SUM([Profit])/SUM([Sales])",
      "datatype": "real"
    }
  ]
}
```

#### `source` / `target` / `mode`

| フィールド | 必須 | 説明 |
|---|---|---|
| `source.luid` | yes | 編集元 PDS LUID |
| `source.kind` | no | `extract` (default) または `live`。download 時の `include_extract` 切替に使う |
| `target.project_id` | no | 出力先 project LUID。省略時は source と同じ project |
| `target.new_name` | yes | 出力 PDS 名。`mode=Overwrite` では source の name と一致必須 |
| `mode` | no | `CreateNew` (default) または `Overwrite` |

#### `transforms[]` (column-level XML 操作)

`column_name` は元 `<column>` の `name` 属性 (内部 ID、bracket 込み)。caption ではなく内部名で参照する (rename で caption が変わっても安定なため)。

| op | 必須フィールド | 動作 |
|---|---|---|
| `rename` | `column_name`, `to_caption` | `<column caption='...'>` を書き換え。`name` は不変。VizQL Metadata API / Workbook / Prep input の全てに反映される |
| `cast` | `column_name`, `to_caption`, `to_datatype` | 元 column に `hidden='true'` を付け、`<calculation class='tableau' formula='<FUNC>([orig_name])'/>` を新規 column として注入。`<FUNC>` は datatype から導出 (`real`→`FLOAT` / `integer`→`INT` / `string`→`STR` / `date`→`DATE` / `datetime`→`DATETIME`)。boolean は default なし、`cast_formula` で明示式が必要 |
| `hide` | `column_name` | `<column>` に `hidden='true'` を追加。VizQL field 一覧から消える (Workbook / Prep input の picker からも消える) |

`cast` のオプションフィールド: `cast_formula` (default の `FUNC(orig)` を上書き), `role` (default は to_datatype から導出), `type` (default は role から導出)。

#### `calcs[]` (任意の派生列注入、既存挙動)

| フィールド | 必須 | 説明 |
|---|---|---|
| `caption` | yes | ユーザー可視 calc 名 |
| `formula` | yes | Tableau Calc 構文の式。caller 提供必須 |
| `datatype` | yes | `real` / `integer` / `string` / `boolean` / `date` / `datetime` |
| `role` | no | `measure` (default for numeric/datetime) / `dimension` |
| `type` | no | `quantitative` (default for measure) / `nominal` / `ordinal` |

### 出力

- Tableau Cloud に新規 (または上書き) PDS が publish される
- ローカル `<out-dir>/`:
  - `original.tdsx` — revert 用のオリジナル DL
  - `original.tds` / `edited.tds` / `verified.tds` — 編集前後の比較用 XML
  - `edited.tdsx` — publish された .tdsx の現物
  - `verified.tdsx` — publish 後に再 DL した .tdsx
- stdout 最終行に `RESULT_JSON: {...}` を emit。フィールド: `published_luid` / `published_name` / `source_kind` / `transforms_applied` / `calcs_injected` (cast op が生成した synthetic calc も含む合計数) / `verified` / `transforms[]` / `calcs[]` / `next_step_recommendation`

### 副作用と承認

| 段階 | 副作用 | 承認の取り方 |
|---|---|---|
| source DL | Cloud 読み取りのみ | 不要 |
| local 編集・re-zip | ローカルファイル生成のみ | 不要 |
| publish (CreateNew) | 新規 PDS 1 個追加 | caller が spec で `new_name` + `target.project_id` を明示済み前提 |
| publish (Overwrite) | 既存 PDS の破壊的更新 | より強い承認が必要。デフォルト挙動にしないため明示 `mode=Overwrite` 必須 |
| 再 DL 検証 | Cloud 読み取りのみ | 不要 |

`Overwrite` は対象 PDS を消費している既存 workbook を破壊する可能性があるため、caller が下流影響を理解した上で指定すること。

## ワークフロー

1. spec を読み込み、必須フィールドと許容値を検証
   - `source.kind` ∈ `{extract, live}`
   - `mode=Overwrite` なら source の PDS 名 == `new_name`
   - `transforms[].op` ∈ `{rename, cast, hide}`、`cast.to_datatype` ∈ 許容セット、`cast.to_caption` と `calcs[].caption` がグローバルに一意
   - `transforms[]` と `calcs[]` の少なくとも一方が非空
2. source PDS を download (`include_extract` は `source.kind` から導出: `extract` → True, `live` → False) → `<out-dir>/original.tdsx`
3. .tdsx を unzip し .tds XML を取り出す → `<out-dir>/original.tds`
4. transforms を適用 (順序固定):
   1. **rename**: `<column>` の `caption` 属性を `to_caption` に書き換え
   2. **hide**: `<column>` に `hidden='true'` を追加
   3. **cast**: 元 `<column>` に `hidden='true'` + `<column><calculation formula='<FUNC>(orig_name)'/></column>` を構築 (synthetic calc として後段の inject パイプラインに渡す)
5. transforms 適用後の可視 caption 空間 (`hidden='true'` を除く `<column caption>`) を計算し、synthetic calc + `calcs[]` の caption と衝突しないか検証 (衝突→ caller に caption 変更要求)
6. 全 calc (synthetic + user) の `name` を `[Calculation_<base + i>]` (base = unix-ms) で連番採番
7. calc XML 群を `<aliases enabled='yes' />` 直後 (無ければ `</datasource>` 直前) に挿入 → `<out-dir>/edited.tds` → `<out-dir>/edited.tdsx`
8. REST `POST /datasources` で publish
   - `mode=CreateNew` で同名既存ありなら HTTP 409 → caller に escalation
   - `mode=Overwrite` で同名 PDS を置換
9. publish 成功後、新 LUID で `include_extract=False` 再 DL → `<out-dir>/verified.tds`
10. 検証:
    - **transforms**: 各 `<column>` の状態 (rename → 新 caption / cast → 元 column が hidden / hide → hidden='true') が verified.tds に残存
    - **calcs**: 各 calc の `name` / `caption` / formula operands が verified.tds に残存
11. `RESULT_JSON` 行を emit。何か MISS なら exit 3、`edited.tdsx` を保持して escalation

VizQL 層での最終確認 (cast op が本当に `dataType: REAL` で exposure されているか等) は Skill 内では行わない。caller が `mcp__tableau__get-datasource-metadata` を published_luid に対して叩いて assert する。

## XML 編集の形

詳細仕様: [references/tds-calc-field-format.md](references/tds-calc-field-format.md)

要点:
- 新規 calc の `<column>` は `<datasource>` 直下、`</connection>` の外側。`name` は opaque `[Calculation_<int>]` で本 Skill が unix-ms 連番採番
- `cast` op は内部的に hide + calc 注入の組合せ (= 2 種の XML 操作を 1 op で表現)
- rename / hide は既存 `<column>` の属性書き換えのみ。`<metadata-records>` には触らない (vconn 由来の生 metadata で server 同期されないため)
- `<column datatype>` の override 単独では VizQL Metadata API / query 層に届かない (cosmetic only)。型キャストが必要なら `cast` op を使う

## 失敗時の対処

| 症状 | 原因 | 対処 |
|---|---|---|
| spec validation fail | 必須フィールド欠落 / datatype 不正 / 同一 spec 内 caption 重複 / transforms と calcs 両方空 | caller に spec 修正を要求 |
| `transforms.column_name` が source にない | caller が指定した内部名が typo / 存在しない | source .tds の `<column name='...'>` を確認して spec 修正 |
| caption 衝突 (post-transform 可視列と) | rename / cast / calc の新 caption が衝突 | 衝突列を hide で隠すか、caption を変える |
| HTTP 409 on publish (CreateNew) | 同名 PDS が既存 | `new_name` を変えるか `mode=Overwrite` を明示指定 |
| HTTP 400 on publish | .tdsx XML 不整合 / formula 構文エラー | `<out-dir>/edited.tds` を保持して inspect、formula 構文を caller に確認 |
| HTTP 401/403/5xx | 認証 / 権限 / Cloud 障害 | escalation (AI では回復不可) |
| 検証で transform / calc が missing | Cloud が編集を silent drop した | サーバー挙動変化の可能性。`edited.tdsx` を保持し escalation |
| `Overwrite` 指定で source ≠ new_name | mode/name の組合せ不整合 | spec を修正 (Overwrite では source.name == new_name) |

## How to invoke

| 指示 | 動作 |
|---|---|
| 「PDS に calc field を足して」 | caller から calc 仕様を受け取り `calcs[]` 形式の spec で `augment_pds.py` を実行 |
| 「<PDS> を base に calc 込みの新 DS を作って」 | `mode=CreateNew` で `new_name` を確認 → augment |
| 「Live PDS の <列> を `<新名>` にリネームして」 | `transforms[]` の `rename` op で spec を組む |
| 「Live PDS の <列> を `<型>` にキャストして」 | `transforms[]` の `cast` op (元列を hide + cast calc を新列として注入)。caller に `to_caption` を確認 |
| 「Live PDS の <列> を非表示にして」 | `transforms[]` の `hide` op |
| 「stg PDS を <vconn 元 PDS> から作って」 | rename + cast + hide を組合せた `transforms[]` を構築。**naming 規約 (snake_case 化等) は caller が `to_caption` を 1 列ずつ明示** (Skill 側で自動変換しない) |

caller が calc 仕様 (caption / formula / datatype) や transforms 仕様 (column_name / to_caption / to_datatype) を提示しない場合は **聞き返す** (auto-detect しない)。formula は業務知識依存、naming は規約依存のため Skill 側で推論しない。

## 認証

`.env` から `SERVER` / `SITE_NAME` を読み、OAuth 2.0 (Authorization Code + PKCE) でブラウザサインインして access_token を取得。Repo 直下の `tableau_auth.py` を共通モジュール (`signed_in_server()` context manager) として import (本 Skill の script から相対 path で参照)。

```
SERVER=https://<your-pod>.online.tableau.com
SITE_NAME=mysite
OAUTH_CALLBACK_PORT=8765   # optional
```

## Scripts

| スクリプト | 役割 |
|---|---|
| `scripts/augment_pds.py` | spec を読み、DL → transforms 適用 → calc inject → publish → verify を一気通貫実行 (非対話、終了時に RESULT_JSON 行を emit) |

スクリプトは単独で動く: Skill 経由でも、ユーザーが `python augment_pds.py --spec spec.json --out-dir <dir>` で直接呼んでも同じ動作。

## 設計原則

- column 追加 (calc) と column 属性編集 (transforms: rename / hide / cast) のみ。column 削除は scope 外 (hide で suppress に留める)
- caller が calc 仕様 / transform 仕様を明示提供する前提。Skill は formula / naming 規約を推論しない
- CreateNew がデフォルト、Overwrite は明示指定必須 (破壊的副作用回避)
- 編集後は必ず round-trip 検証 (再 DL して transform / calc が survive したか機械チェック)
- 失敗は握り潰さない (HTTP status / 検証結果をそのまま caller に返す)
- .hyper のデータ本体は触らない (XML 編集のみで派生列・型変更を表現)
- ローカル成果物は revert 可能なように `original.tdsx` を必ず保管
- VizQL 層の最終 assertion (cast の `dataType` 等) は Skill 内に持たず caller (Metadata API) に委譲する
