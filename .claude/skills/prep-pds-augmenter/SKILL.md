---
name: prep-pds-augmenter
description: Tableau Cloud / Server 上の Published Data Source を Calculated Field 注入と column-level transforms (rename / cast / hide) で機械的に改変・量産する Skill。source は extract (Hyper-backed) / live (既存 Live PDS) / vconn (仮想接続から base .tds をゼロ合成) の 3 種で、.tds XML 編集 → publish → 再 DL 検証を一気通貫で実行する。Prep フローが publish した Hyper Output に派生列を足したいとき、分解元 Prep の vconn Input から stg 相当の Live PDS を publish したいとき、既存 Live PDS に BI 向けの rename / cast / hide を当てたいとき、「PDS に calc を注入して」「calc 込み PDS を量産して」と言われたときに起動。calc / transform 仕様と vconn 列メタは caller が明示提供する前提で auto-detect しない。
---

# prep-pds-augmenter

Tableau Cloud / Server 上の Published Data Source (PDS) を **transforms** (rename / cast / hide) と **calc 注入** で機械的に改変・量産する Skill。extract-based (Hyper) と live-connection (virtual connection backed) の既存 PDS、および「既存 PDS なしで vconn から base .tds をゼロから組み立てる」vconn ソースの 3 種を扱える。

主な用途:
- Prep flow が出力した Hyper PDS に汎用的な派生列 (利益率 / 換算金額 / 閾値フラグ) を後付けで足す
- **分解元 Prep の Input が仮想接続だった場合に、stg 相当の Live PDS を vconn から直接 publish** する (`source.kind: "vconn"`、既存 base PDS が不要)
- 仮想接続経由の既存 Live PDS に対して BI 向けの rename / cast / hide を XML 編集で当てる (`source.kind: "live"`)
- composable PDS 公開時に派生列込みの PDS を量産する編集パイプライン

## rename semantics (束縛層の契約)

下流 Prep flow (LoadSqlProxy) は PDS のフィールドを **local-name** で束縛し、caption は BI / VizQL の表示専用。この分離により rename の semantics は source kind で異なる:

| source kind | rename の実体 | 下流 Prep から新名で読めるか |
|---|---|---|
| `vconn` | **true rename**: caption + local-name 書き換え + `<cols><map>` で物理列へマッピング | ✅ 読める (stg-as-Live-PDS が成立する唯一の経路) |
| `extract` / `live` | caption-only (`name` 不変)。既存 consumer の field 参照を壊さないため | ❌ 旧名のまま。Prep 消費前提なら stg を実 .tfl で作る |

書き込み副作用 (新規 PDS 作成 or 既存 PDS 上書き) を伴うため、caller が target / new name / transforms / calcs / (vconn 時は) 列メタを明示合意してから呼ぶ前提。

## スコープ

含む:
- extract-based (Hyper-backed) PDS への calc field 注入 (`source.kind: "extract"`、default)
- live-connection PDS への calc field 注入 + transforms (`source.kind: "live"`)
- **vconn から base .tds をゼロから組み立てて publish** (`source.kind: "vconn"`、source PDS の DL 不要、caller 提供の列メタから `<connection class='federated'>` + `<publishedConnection>` + `<relation>` + `<metadata-records>` + `<column>` を合成)
- transforms 操作: **rename** (caption 書き換え) / **cast** (hidden + cast calc) / **hide** (`hidden='true'`)
- CreateNew で別 PDS として publish (default、安全、vconn では唯一の選択肢)
- Overwrite で同名 PDS を XML 差分のみで置換 (明示指定時のみ、extract/live のみ)
- 編集後の round-trip 検証 (再 DL して transform / calc が残ったか確認)

含まない:
- 既存 calc field の **編集・削除** — 注入のみ
- 既存 column の **削除** — vconn / extract schema との整合を崩すリスクが高いので hide で suppress に留める
- formula の auto-detect / 推論 — caller 提供必須 (沈黙 fallback 回避)
- caption の naming 規約自動変換 (snake_case 化等) — caller が `to_caption` を 1 列ずつ明示提供
- .hyper のデータ本体の変更 — XML 編集のみで派生列・型変更を表現
- VizQL Metadata API での型 assertion — Skill 内 verify は .tds XML round-trip まで。VizQL 層の最終確認 (calc が `dataType: REAL` / `columnClass: CALCULATION` で見えているか等) は caller が `mcp__tableau__get-datasource-metadata` で別途行う前提 (AVG/SUM 値による型推定は Tableau の auto-promotion で判別不能なので使わない)

## 動作モデル

1 サイクル = (source 1 個: PDS LUID または vconn 参照) + (transforms M 個) + (calc spec N 個) → (target PDS 1 個 publish)。

`python ${CLAUDE_SKILL_DIR}/scripts/augment_pds.py --spec spec.json --out-dir <dir>` で起動。spec.json の全フィールド (source 3 種別の必須/任意、transforms / calcs / source.columns / 出力ファイル一覧) は [references/spec-format.md](references/spec-format.md) を参照。

### 副作用と承認

| 段階 | 副作用 | 承認の取り方 |
|---|---|---|
| source DL (extract/live) / synthesize (vconn) | Cloud 読み取りのみ または ローカル合成のみ | 不要 |
| local 編集・re-zip | ローカルファイル生成のみ | 不要 |
| publish (CreateNew) | 新規 PDS 1 個追加 | caller が spec で `new_name` + `target.project_id` を明示済み前提 |
| publish (Overwrite) | 既存 PDS の破壊的更新 | より強い承認が必要。デフォルト挙動にしないため明示 `mode=Overwrite` 必須 (vconn では使用不可) |
| 再 DL 検証 | Cloud 読み取りのみ | 不要 |

`Overwrite` は対象 PDS を消費している既存 workbook を破壊する可能性があるため、caller が下流影響を理解した上で指定すること。

## ワークフロー

spec 検証 → base .tdsx 取得 (extract/live は DL / vconn は caller 提供メタからゼロ合成) → transforms (rename / hide / cast) を順序固定で適用 → calc 注入 → publish → 再 DL で round-trip 検証 → `RESULT_JSON` emit。ローカル成果物は revert 可能なように `original.tdsx` を必ず保管する。XML 編集の詳細順序・calc ID 採番・検証ロジックは [references/tds-calc-field-format.md](references/tds-calc-field-format.md) を参照。

## 失敗時の対処

3 系統: (1) spec validation error → caller に spec 修正要求 / (2) HTTP エラー (publish 4xx/5xx) → 4xx は caller 入力、5xx は escalation / (3) round-trip 検証 MISS → `edited.tdsx` を保持し escalation。Exit code (0/1/2/3) と症状別対処の完全な表は [references/failure-modes.md](references/failure-modes.md) を参照。

## How to invoke

| 指示 | 動作 |
|---|---|
| 「PDS に calc field を足して」 | caller から calc 仕様を受け取り `calcs[]` 形式の spec で `augment_pds.py` を実行 |
| 「<PDS> を base に calc 込みの新 DS を作って」 | `mode=CreateNew` で `new_name` を確認 → augment |
| 「Live PDS の <列> を `<新名>` にリネームして」 | `transforms[]` の `rename` op で spec を組む |
| 「Live PDS の <列> を `<型>` にキャストして」 | `transforms[]` の `cast` op (元列を hide + cast calc を新列として注入)。caller に `to_caption` を確認 |
| 「Live PDS の <列> を非表示にして」 | `transforms[]` の `hide` op |
| 「既存 Live PDS を base に stg PDS を作って」 (**base PDS が既にある**) | `kind: live` で rename + cast + hide を組合せた `transforms[]` を構築。rename は caption-only なので**下流 Prep は旧 local-name のまま読む** (kind:vconn との対比) |
| 「分解元 Prep の vconn 入力から stg PDS を作って」 (**既存 base PDS なし**) | `kind: vconn` で `vconn_luid` / `table_uuid` / `table_name` / `columns[]` を caller (= 通常 prep-builder) から受け取り、transforms[] と共に spec 化。caller が flow.json の Input ノードから列メタを揃える前提。rename は true rename なので**下流 Prep も新名で読める** (kind:live との対比) |

caller が calc 仕様 (caption / formula / datatype)、transforms 仕様 (column_name / to_caption / to_datatype)、vconn 列メタ (name / caption / datatype) を提示しない場合は **聞き返す** (auto-detect しない)。formula は業務知識依存、naming は規約依存、vconn 列スキーマは flow.json or vconn schema API 依存のため Skill 側で推論しない。

## 認証

OAuth 2.0 (Authorization Code + PKCE) のブラウザサインイン。詳細は [prep-deployer/references/authentication.md](../prep-deployer/references/authentication.md)。

## Scripts

| スクリプト | 役割 |
|---|---|
| `scripts/augment_pds.py` | spec を読み、DL → transforms 適用 → calc inject → publish → verify を一気通貫実行 (非対話、終了時に RESULT_JSON 行を emit) |

スクリプトは単独で動く: Skill 経由でも、ユーザーが `python ${CLAUDE_SKILL_DIR}/scripts/augment_pds.py --spec spec.json --out-dir <dir>` で直接呼んでも同じ動作。失敗は握り潰さない (HTTP status / 検証結果をそのまま caller に返す)。

設計原則は §スコープ (含む / 含まない)・§動作モデル (副作用と承認)・§ワークフロー に集約済み — 別リストとして再掲しない。
