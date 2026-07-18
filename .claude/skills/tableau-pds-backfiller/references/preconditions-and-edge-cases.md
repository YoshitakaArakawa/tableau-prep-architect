---
purpose: tableau-pds-backfiller の着手前 refuse 条件と、seam/replace・非1:1スキーマ・idempotency・timezone・事後 parity の edge case を規定する
note: recipe の Step 0 (前提条件) とゲート① (seam/replace) の判断根拠。backfill が本番でデータ破損・二重化を起こさないための境界条件集
---

# 前提条件と edge case

## 目次

- refuse 条件 (着手前に中止)
- seam / replace の選択
- 非 1:1 スキーマの扱い
- idempotency と二重 append ガード
- control field の timezone / DATE vs DATETIME
- 事後 parity の定義 (comparator への橋渡し)
- 保留: passthrough accumulator の層設計
- 大規模 extract のスケール

## refuse 条件 (着手前に中止)

次のいずれかに該当したら **escalate して backfill を中止する** (recipe Step 0)。

- **対象が full-refresh 出力**: `resolve_accumulator.py` の classification が `accumulator` でない (`full_refresh` / `inert_incr` / `append_only`)。full-refresh 出力に seed しても次回 run で上書きされて消える。`inert_incr` (control / outputNodeId が空の UI 残骸) も Prep は full 扱いする
- **旧 PDS が不在**: `old_luid` が解決できない / 既に削除済み。履歴の供給源が無いので backfill 不能。旧 PDS は backfill 確定まで削除しないこと
- **スキーマが吸収不能**: `diff_pds_schema.py` の差分が rename (`--rename` / `column_map`) と cast で吸収できない (意味的に対応しない列、対応先の無い新規列)。無理に挿入すると NULL 汚染や型崩れになる
- **下流が cross-day 履歴に依存**: gap 期間中に劣化する派生 (DoD / WoW、window LOOKUP、移動平均、累積ランク等) を下流が持つ。backfill で埋める前提が壊れていないか業務確認が要る
- **層設計が未決の passthrough** (下の「保留」節): 対象 PDS 名が変わりうるので、決着まで保留

## seam / replace の選択

**フロー単位のユーザー判断** (recipe ゲート①)。自動判定に委ねず、baseline の中身を提示して選ばせる。

| モード | 使う条件 | 挿入内容 | watermark (MAX control) |
|---|---|---|---|
| `seam` (既定) | 新 accumulator が **正しい baseline** (最新バッチの実データ) を持つ | 旧の `control < MIN(新の control)` の行のみ | 不変 (過去行のみ追加) |
| `replace` | 新が **sentinel/placeholder** しか持たない (control が実データ範囲外の far-past 値等) | 新を DELETE して旧を全ロード | old_max |

- **sentinel の見分け方**: 新の `control` の distinct が 1、かつ新の control 全体が旧の最古より前 (`new_max <= old_min`)。この場合 seam 規則は `control < seam` = 0 行になり seed できない。`backfill_pds.py` は dry-run でこれを `sentinel_warning: true` として surface する
- seam モードで重複区間 `[seam, new_max]` は **新 baseline を正として温存**する (旧で上書きしない)。新は最新の source から作られた確定値なので、旧の同区間より信頼できる

## 非 1:1 スキーマの扱い

分解で列が増減した場合 (旧 ⊋ 新 / 旧 ⊊ 新):

- **旧にあり新に無い列** (`only_old`): 挿入時に落とす。新の INSERT 列リストに無いので自然に捨てられる (name-align の帰結)
- **新にあり旧に無い列** (`only_new`): 対応する旧列が無い → `backfill_pds.py` は **escalate** する。v1 は「新規列に default / NULL を自動で与える」ことはしない (沈黙 fallback 回避)。対応が必要なら `column_map` で既存旧列を割り当てるか、backfill 自体を設計に差し戻す
- **型不一致** (`type_mismatch`): `column_map` の `cast` で明示変換する。cast を与えずに型が違うと挿入が失敗する

## idempotency と二重 append ガード

- **seam モードは自己冪等**: backfill 後は新 MIN が旧 MIN まで下がるので、再実行しても `control < MIN(新)` = 0 行 = 挿入なし。replace も truncate+reload で冪等
- **ただし backfill の間に full run が挟まると二重化する**: append 出力を full で回すと現行スナップショット全体が再追記される。これは backfill 固有ではなく accumulator 全般の規律 (seed 後は必ず incremental run のみ)
- このため recipe Step 1 の interlock で **in-flight run 無し** を必須にし、Step 5 直前に再確認する。schedule は**時刻ベース**で見る: 次回 run が操作 window 内に迫る場合のみ suspend が要る。window 外の active schedule は suspend 不要 — seam は watermark を保存するので、操作後に走る Incremental scheduled run は二重化しない (二重化するのは Full run だけで、それは backfill と無関係な既存不備)。冪等性は「同じ backfill の再実行」に対してのみ成り立ち、「間に挟まる full run」に対しては壊れる

## control field の timezone / DATE vs DATETIME

- seam 比較と挿入は Hyper SQL 内で完結する (`INSERT ... SELECT ... WHERE control < (SELECT MIN(control) FROM new)`)。旧・新の control は同じ Hyper 型なので **比較は型ネイティブで行われ、リテラル整形も TZ 変換も挟まらない** (Python 側で日時をフォーマットしないので DST バグが入らない)
- 前提: 旧・新の control の **型が一致していること** (`diff_pds_schema.py --control` で確認)。DATETIME (intraday 時刻を持つ) と DATE では seam の day 境界の扱いが変わる:
  - DATE control: seam は日単位。`control < seam` は「seam の日を含まない」
  - DATETIME control: seam は時刻込み。同じ日でも seam 時刻より前の行だけが挿入される (intraday の取りこぼしに注意)
- 型が旧・新で違う場合は `column_map` の `cast` で揃えるが、DATE↔DATETIME の cast は境界の意味が変わるので **ユーザー確認**を挟む

## 事後 parity の定義 (comparator への橋渡し)

- backfill 前は「control 期間の重なる範囲」でしか parity を取れないが、**backfill 後は全期間の行数 parity が再び有効**になる (旧の履歴が新に入るため)
- 挿入行は旧行を **name-align で逐語コピー**したものなので、**値同値は構造的に保証**される (comparator が値比較まで踏み込まなくても、行数 parity が一致すれば内容一致とみなせる)
- 事後 parity の取り方: 対 旧総数 (replace) または 旧総数 + 新 baseline 増分 (seam) を期待値に、[tableau-pds-comparator](../../tableau-pds-comparator/SKILL.md) の行数差分に渡す。`backfill_pds.py` の `server_verify` が既にサーバー実測の行数・MAX(control) を出しているので、それを期待値と突合すれば一次確認になる

## 保留: passthrough accumulator の層設計

int 層を持たない single-table passthrough の accumulator は構造上 mart に居る。これを int 化するか (命名・層の設計) は backfill とは別レイヤの判断で、**別タスクに切り出す**。その決着まで当該フローの backfill は保留する — int 化で対象 PDS 名 (= `new_luid` が指す name) が変わりうるので、確定前に seed すると対象を取り違える。

## 大規模 extract のスケール

- 行は Python に読み込まず、Hyper の `attach_database` + `INSERT INTO new SELECT ... FROM old` でエンジン内転送する。数百万行 accumulator でもメモリに依存しない
- publish サイズは `tableauserverclient` が 64MB チャンクに分割して送るので、.tdsx サイズの上限はサーバー側の datasource サイズ制限に従う (Skill 側で追加の streaming 制御はしない)
