---
purpose: tableau-pds-augmenter (augment_pds.py) の失敗種別と対処の一覧。caller が exit code と RESULT_JSON / stderr から原因を分類するための reference
note: 認証 / 権限 / Cloud 障害以外は recoverable - spec を直すか入力メタを直すかで再実行可能
---

# 失敗時の対処

## Exit code 一覧

| code | 原因カテゴリ | 副作用 |
|---|---|---|
| 0 | 成功 (publish + verify 両方通過) | PDS が publish 済み |
| 1 | spec validation error / caption 衝突 / unknown column reference | publish 未発生 |
| 2 | Tableau からの HTTP エラー (publish 4xx/5xx) | publish 失敗、`edited.tdsx` は保持 |
| 3 | round-trip 検証失敗 (publish はしたが transform/calc が verify で missing) | PDS は publish 済みだが内容が想定外 - 確認要 |

## 症状別対処

| 症状 | 原因 | 対処 |
|---|---|---|
| spec validation fail | 必須フィールド欠落 / datatype 不正 / 同一 spec 内 caption 重複 / transforms と calcs 両方空 (extract/live のみ) / vconn 必須フィールド欠落 / vconn + Overwrite | caller に spec 修正を要求 |
| `transforms.column_name` が source にない | caller が指定した内部名が typo / 存在しない (vconn では `source.columns[].name` に列挙されていない) | source .tds の `<column name='...'>` (extract/live) または `source.columns[].name` (vconn) を確認して spec 修正 |
| caption 衝突 (post-transform 可視列と) | rename / cast / calc の新 caption が衝突 | 衝突列を hide で隠すか、caption を変える |
| HTTP 409 on publish (CreateNew) | 同名 PDS が既存 | `new_name` を変えるか `mode=Overwrite` を明示指定 (vconn 時は new_name を変えるしかない) |
| HTTP 400 on publish | .tdsx XML 不整合 / formula 構文エラー / vconn 参照不正 (vconn_luid 不正・table 不在等) | `<out-dir>/edited.tds` を保持して inspect、formula 構文と vconn メタを caller に確認 |
| HTTP 401/403/5xx | 認証 / 権限 / Cloud 障害 | escalation (AI では回復不可) |
| 検証で transform / calc が missing | Cloud が編集を silent drop した | サーバー挙動変化の可能性。`edited.tdsx` を保持し escalation |
| `Overwrite` 指定で source ≠ new_name | mode/name の組合せ不整合 | spec を修正 (Overwrite では source.name == new_name) |
| `vconn` 指定で `mode=Overwrite` | vconn は base から合成する用途で Overwrite 対象が存在しない | spec を `mode=CreateNew` に修正 |

## 回復可能 vs 不能の境界

- **回復可能** (caller が spec / 列メタを直して再実行可能): exit 1 全部 / HTTP 400 / HTTP 409 / 検証 MISS で原因が transform 仕様にある場合
- **回復不能** (Skill 内では対処せず escalation): HTTP 401/403/5xx / 検証 MISS で原因不明 (Cloud 挙動変化の疑い) / 認証セッション喪失

## 失敗時に保持されるローカル成果物

`<out-dir>/` 配下:
- `original.tdsx` / `original.tds` — 編集前 (DL 直後 or vconn 合成直後)
- `edited.tds` / `edited.tdsx` — publish 試行に使った XML (HTTP エラーや検証 MISS の inspection 用)
- `verified.tds` / `verified.tdsx` — verify 段階で再 DL したもの (verify 失敗の差分確認用)

これらが残っているので、caller は exit ≠ 0 のとき `edited.tds` と `verified.tds` を diff して原因切り分けできる。
