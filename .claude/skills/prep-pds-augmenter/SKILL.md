---
name: prep-pds-augmenter
description: Tableau Cloud / Server に publish 済みの extract-based Published Data Source に、Tableau Desktop 形式の Calculated Field を後付けで注入する Skill。download → .tdsx 内の .tds XML に <column><calculation/></column> を挿入 → CreateNew (デフォルト) または Overwrite モードで REST API 経由で publish → 再 DL して calc が survive したか機械検証、を一気通貫で行う。Prep フローが publish した Hyper Output に汎用的な派生列 (利益率 / 換算金額 / 閾値フラグ等) を後付けで足したいとき、calc 込みの PDS を機械的に量産したいとき、Composable PDS 公開時に備えて編集パイプラインを用意しておきたいときに起動。caller が calc 仕様 (caption / formula / datatype) を明示的に与える前提で、formula の auto-detect はしない。
---

# prep-pds-augmenter

Tableau Cloud / Server 上の **extract-based** Published Data Source (PDS) に Calculated Field を後付けで注入する Skill。flow が出力した Hyper PDS を「派生列込み」の PDS に量産することが主目的。

書き込み副作用 (新規 PDS 作成 or 既存 PDS 上書き) を伴うため、caller が target / new name / calcs を明示合意してから呼ぶ前提。

## スコープ

含む:
- extract-based (Hyper-backed) PDS への calc field 注入
- CreateNew で別 PDS として publish (default、安全)
- Overwrite で同名 PDS を XML 差分のみで置換 (明示指定時のみ)
- 注入後の round-trip 検証 (再 DL して calc が残ったか確認)

含まない:
- live-connection PDS (federated 非 extract) — 未検証
- 既存 calc field の **編集・削除** — 注入のみ (将来拡張で考慮)
- formula の auto-detect / 推論 — caller 提供必須 (沈黙 fallback 回避)
- .hyper のデータ本体の変更 — XML 編集のみで派生列を表現

## 動作モデル

1 サイクル = (source PDS 1 個) + (calc spec N 個) → (target PDS 1 個 publish)

### 入力

`spec.json` 形式で渡す:

```json
{
  "source": { "luid": "<src-pds-luid>" },
  "target": { "project_id": "<target-project-luid>", "new_name": "fct_sales_with_calcs" },
  "mode": "CreateNew",
  "calcs": [
    {
      "caption": "Profit Ratio",
      "formula": "SUM([Profit])/SUM([Sales])",
      "datatype": "real",
      "role": "measure",
      "type": "quantitative"
    }
  ]
}
```

| フィールド | 必須 | 説明 |
|---|---|---|
| `source.luid` | yes | 注入元 PDS LUID |
| `target.project_id` | no | 出力先 project LUID。省略時は source と同じ project |
| `target.new_name` | yes | 出力 PDS 名。`mode=Overwrite` では既存 PDS 名と一致させる |
| `mode` | no | `CreateNew` (default) / `Overwrite` |
| `calcs[].caption` | yes | ユーザー可視 calc 名 |
| `calcs[].formula` | yes | Tableau Calc 構文の式。caller 提供必須 |
| `calcs[].datatype` | yes | `real` / `integer` / `string` / `boolean` / `date` / `datetime` |
| `calcs[].role` | no | `measure` (default) / `dimension` |
| `calcs[].type` | no | `quantitative` (default for measure) / `nominal` / `ordinal` |

### 出力

- Tableau Cloud に新規 (または上書き) PDS が publish される
- ローカル `<out-dir>/`:
  - `original.tdsx` — revert 用のオリジナル DL
  - `original.tds` / `edited.tds` / `verified.tds` — 注入前後の比較用 XML
  - `edited.tdsx` — publish された .tdsx の現物
  - `verified.tdsx` — publish 後に再 DL した .tdsx
- stdout 最終行に `RESULT_JSON: {"published_luid": "...", "published_name": "...", "calcs_injected": N, "verified": true}` を emit

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
   - `mode=Overwrite` の場合は source の PDS 名と `new_name` の一致を確認
   - `calcs[].datatype` が許容セットに含まれるか
   - `calcs[].caption` の重複 (同一 spec 内) が無いか
2. source PDS を `include_extract=True` で DL → `<out-dir>/original.tdsx`
3. .tdsx を unzip し .tds XML を取り出し、各 calc 仕様について:
   - `name` 属性を `[Calculation_<unix-ms>]` で自動採番
   - `<column caption='...' datatype='...' name='[...]' role='...' type='...'><calculation class='tableau' formula='...' /></column>` を構築
4. .tds 内の挿入位置を決定:
   - 第一候補: `<aliases enabled='yes' />` の直後 (Sample - Superstore.tds と同じ位置)
   - フォールバック: `</datasource>` の直前
5. 既存 column との caption 衝突を検出 (`caption='<...>'` を全 column 走査):
   - 衝突あり → fail し caller に通知 (caption 変更を要求)
6. 編集後 .tds を元 .tdsx に書き戻し → `<out-dir>/edited.tdsx`
7. REST `POST /datasources` で publish
   - `mode=CreateNew` の場合: 同名既存ありなら 409 → caller に escalation
   - `mode=Overwrite` の場合: 同名 PDS を XML 差分のみで置換
8. publish 成功後、新 LUID で再 DL (`include_extract=False`) → `<out-dir>/verified.tds`
9. 検証:
   - 注入した全 calc の `name` (`[Calculation_<unix-ms>]`) が verified.tds に存在
   - 全 calc の `caption` が verified.tds に存在
   - formula の operands (列名・関数名) が verified.tds に存在 (XML escape を考慮)
10. RESULT_JSON 行を emit

検証で一部 calc が missing なら exit 1 し、`<out-dir>/edited.tdsx` を保持して caller に escalation。

## Calc 注入 XML の形

詳細仕様: [references/tds-calc-field-format.md](references/tds-calc-field-format.md)

要点:
- `<column>` は `<datasource>` 直下、`</connection>` の外側に置く
- `name` 属性は opaque ID `[Calculation_<int>]` (Tableau Desktop 互換、本 Skill が unix-ms で採番)
- `caption` がユーザー可視ラベル
- `<calculation class='tableau' formula='...'/>` を子に持つ
- 同 .tds 内の既存 `<column>` 要素 (素のカラム宣言含む) と sibling 関係

## 失敗時の対処

| 症状 | 原因 | 対処 |
|---|---|---|
| spec validation fail | 必須フィールド欠落 / datatype 不正 / 同一 spec 内 caption 重複 | caller に spec 修正を要求 |
| caption 衝突 (既存 column と) | spec の caption が source の既存 column / calc と被る | caller に caption 変更を要求 |
| HTTP 409 on publish (CreateNew) | 同名 PDS が既存 | `new_name` を変えるか `mode=Overwrite` を明示指定 |
| HTTP 400 on publish | .tdsx XML 不整合 / schema mismatch | `<out-dir>/edited.tds` を保持して inspect、formula 構文を caller に確認 |
| HTTP 401/403/5xx | 認証 / 権限 / Cloud 障害 | escalation (AI では回復不可) |
| 検証で calc が missing | Cloud が注入を silent drop した | サーバー挙動変化の可能性。`edited.tdsx` を保持し escalation |
| `Overwrite` 指定で source ≠ new_name | mode/name の組合せ不整合 | spec を修正 (Overwrite では source.name == new_name) |

## How to invoke

| 指示 | 動作 |
|---|---|
| 「PDS に calc field を足して」 | caller から spec を受け取り `augment_pds.py` を実行 |
| 「<PDS 名> に <列名> = <式> の calc を追加して」 | 1-calc 用の short-form を spec に展開 |
| 「<PDS> をベースに calc 込みの新 DS を作って」 | mode=CreateNew で new_name を確認 → augment |

caller が calc 仕様 (caption / formula / datatype) を提示しない場合は **聞き返す** (auto-detect しない)。formula は業務知識依存のため Skill 側で推論しない。

## 認証

`.env` ファイルから PAT を読み込む。Repo 直下の `tableau_auth.py` を共通モジュールとして import (本 Skill の script から相対 path で参照)。

```
SERVER=https://<your-pod>.online.tableau.com
SITE_NAME=mysite
PAT_NAME=...
PAT_VALUE=...
```

## Scripts

| スクリプト | 役割 |
|---|---|
| `scripts/augment_pds.py` | spec を読み、DL → inject → publish → verify を一気通貫実行 (非対話、終了時に RESULT_JSON 行を emit) |

スクリプトは単独で動く: Skill 経由でも、ユーザーが `python augment_pds.py --spec spec.json --out-dir <dir>` で直接呼んでも同じ動作。

## 設計原則

- 注入のみ (編集・削除は scope 外、将来拡張で考慮)
- caller が calc 仕様を明示提供する前提。Skill は formula を推論しない
- CreateNew がデフォルト、Overwrite は明示指定必須 (破壊的副作用回避)
- 注入後は必ず round-trip 検証 (再 DL して calc が survive したか機械チェック)
- 失敗は握り潰さない (HTTP status / 検証結果をそのまま caller に返す)
- .hyper のデータ本体は触らない (XML 編集のみで派生列を表現)
- ローカル成果物は revert 可能なように original.tdsx を必ず保管
