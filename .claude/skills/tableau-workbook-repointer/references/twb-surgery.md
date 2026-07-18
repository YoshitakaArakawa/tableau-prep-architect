---
purpose: repoint モード (TWB 手術による自動差し替え) の機構・publish 契約・検証セット・制約の根拠を集約する
sources:
  - https://github.com/tableau/tableau-document-schemas
fetched_at: 2026-07-18
source_last_known_update: 不明 (取得時点で 2026_1 / 2026_2 の XSD を収録)
note: 手順そのものは SKILL.md、lineage の読み方は lineage-model.md。本ファイルは「なぜ全文置換か」「何が保存され何が未検証か」の機構知識を持つ。XSD は構造の baseline だが connection 属性は非検証領域 (processContents="skip") のため、本ファイルの connection まわりの記述は実 TWB の観測に基づく
---

# TWB 手術モデル

## 目次

- published DS 参照の構造と置換対象
- なぜ全文置換か (capabilities キャッシュ CDATA blob)
- 置換ペアの導出
- publish 契約 (何が保存されるか)
- リハーサル → 本番の段取り
- 検証セット (3 点) と各々の限界
- 制約・未検証事項

## published DS 参照の構造と置換対象

TWB 内で published DS への参照は `<datasource>` 直下の 2 要素で表現される:

```xml
<datasource caption='<表示名>' inline='true' name='sqlproxy.<hash>' version='...'>
  <repository-location id='<content_url>' path='/t/<site>/datasources'
                       derived-from='...<content_url>?rev=...' revision='...' site='<site>' />
  <connection channel='https' class='sqlproxy' dbname='<content_url>'
              server-ds-friendly-name='<表示名>' ... >
```

差し替えに必要な置換はトークン 2 種だけ:

| トークン | 出現箇所 |
|---|---|
| **content_url** | `repository-location` の `id` / `derived-from` URL、`connection` の `dbname` |
| **表示名** | `datasource` の `caption`、`connection` の `server-ds-friendly-name`、worksheet 内 `<datasource caption=...>` |

worksheet のフィールド参照は内部名 `sqlproxy.<hash>` を使っており、これを**変更しなければ**ビューは壊れない。内部名・`revision`・WB レベルの `repository-location`・`saved-credentials-viewerid` は stale のまま残してもサーバーが許容する (publish 時に再解決される)。

## なぜ属性スコープの全文置換か (CDATA blob と表示名==content_url)

置換は **TWB 全文に対する属性スコープの文字列置換** (`id='<token>'` / `dbname='<token>'` /
`/datasources/<token>?` / `caption='<表示名>'` / `server-ds-friendly-name='<表示名>'` の完全一致) を正とする。根拠は 2 つ:

- **要素単位の XML 編集は CDATA blob を見落とす**: `<connection>` 内の `<attribute datatype='string' name='datasource'>` に datasource 定義の直列化コピーが CDATA で埋まっており (capabilities キャッシュ)、旧 content_url がそこにも出現する。blob 内も同じ属性構文なので、全文への属性スコープ置換なら自動的にカバーされる
- **無差別な substring 置換は表示名を壊す**: 表示名と content_url が**同一値**の PDS (よくある) では、content_url の置換パスが caption / server-ds-friendly-name まで消費し、表示名が新 content_url 値 (例: `fct_transactions_summary`) で潰れる。属性スコープなら接続属性と表示名属性が独立し、この衝突が起きない。完全一致照合なので接頭辞衝突の順序調整も不要

置換後に旧トークンの接続属性 (`id=` / `dbname=`) 残存数 0 を機械確認する (`repoint_workbook.py` が実施)。属性外の平文出現 (ワークシートのタイトル文字列等) は置換しない — 機能に影響せず、見た目の直しは人間判断。

## 置換ペアの導出

- **content_url は表示名から導出できない** (例: 表示名 `fct_superstore_orders` に対し content_url は `fct_orders` のように別物になりうる)。必ず REST `datasources.get` で LUID から解決する
- **旧トークンの正は「TWB が実際に参照している値」であり、旧 PDS の現行 content_url ではない**。PDS が再 publish されるとサフィックス付き content_url (例: `<name>_1757...`) に変わるが、WB 側は旧 content_url を参照し続けることがある (Metadata API の lineage にも写らない)。解決順: (1) 現行 content_url が TWB 内にあればそれ、(2) なければ旧表示名と caption 一致する datasource の repository-location id、(3) どちらも無ければ停止 (stale design として design 再実行を促す)。(2) を使った場合は warning で明示する
- **旧・新とも PDS の LUID が確定していることが前提** (`match: "name"` で LUID null のペアは手術不可 → resolve-luids を先に回す)

## publish 契約 (何が保存されるか)

- DL は `include_extract=False` でも `.twbx` で届くことがある → 展開して `.twb` を取り出す。**publish は `.twb` 単体で可** (再 zip 不要)
- `PublishMode.Overwrite` で**同名・同プロジェクト**に publish すると **WB の LUID と webpage URL は不変** → 埋め込み URL・権限設定が生き残る
- `show_tabs` 等の publish パラメータは `WorkbookItem` に**再指定しないと失われる** — publish 前に `workbooks.get_by_id` で実値を取り、同値を渡す
- Overwrite は対象が無ければ新規作成する (リハーサル publish の再実行が冪等になる根拠)

## リハーサル → 本番の段取り

本番 WB は blast radius が大きいため、**リハーサルを飛ばして本番 Overwrite しない**:

1. **rehearsal**: 手術済み TWB をリハーサル用プロジェクトに別名 (`rehearsal_<元名>`) で publish。元 WB は無傷のまま
2. **証拠取得**: 元 WB (baseline) × リハーサル copy (candidate) の view 別 CSV + 画像比較 (`compare_workbook_views.py`)、接続チェック (旧 PDS 名の残存ゼロ)
3. **承認レポート**: 機械出力を `render_rehearsal_report.py` で repoint-rehearsal-report.html (+.md) に join。機械判定 (`READY_FOR_APPROVAL` / `NOT_READY`) は「接続切替 + copy 側の全 view export 成功」のみを保証し、行数と画像並置 (埋め込み) は人間の目視確認材料とする。データ同値性はこのゲートで再判定しない
4. **ユーザー承認**: レポートを提示して明示承認を得る (fork 内では対話できないため、rehearsal と production は**別 invocation** に分ける)
5. **production**: 同じ手術済み内容を元 WB へ Overwrite publish
6. **verify**: lineage 再走査 (verify モード) + 必要なら本番 WB × リハーサル copy の再比較

## 検証セット (3 点) と各々の限界

| 検証 | 手段 | 判定できること | 限界 |
|---|---|---|---|
| 接続チェック | REST workbook connections の `datasource_name` | 旧 PDS 参照 (表示名 / 旧トークン) の残存ゼロが主。新 PDS の出現は「新表示名 **または** 新 content_url」で判定 | **`datasource_name` は表示名を返すことも content_url 由来名を返すこともある** (表示名一致だけで照合すると偽 FAIL)。`datasource id` も PDS 本体 LUID ではない (shadow id)。LUID 級の確証は lineage で取る |
| render/export チェック | view 別 CSV (live query) + 画像 (fresh render) の export | copy 側の全 view が新 PDS 経由で描画・クエリ実行できる (**copy 側の export 失敗のみブロッキング**。baseline 側だけの失敗は元 WB の既存破損サイン = 改善確認) | export が通ること以上のデータ検証はしない |
| 行数・画像並置 (目視材料) | 上記 export の行数集計とサムネイル埋め込み | 人間が「大きな崩れがないか」をひと目で確認する | **値の等価性はこのゲートで判定しない** — 旧 PDS vs 新 PDS の parity は repoint の事前条件として tableau-pds-comparator が検証済み (§制約)。refresh タイミング差による値の微差は想定内 |

lineage (downstreamWorkbooks) の再走査は verify モードの管轄 ([lineage-model.md](lineage-model.md))。

## 制約・未検証事項

- **タグ・説明・Custom Views・購読が Overwrite publish で保持されるかは未検証**。重要 WB では本番前に人間が控えを取るか、リハーサル copy で挙動を確認する
- 1 WB 内の**複数接続の同時差し替え**は置換ペアを増やすだけの設計だが、多接続 WB での実績は接続 1 本のケースより薄い — リハーサル比較を必ず全ビューで見る
- **列パリティが前提**: 新 PDS に旧と同名の列が揃っていること (tableau-pds-comparator の schema diff PASS を事前条件にする)。列名が違う移行では worksheet のフィールド参照が壊れる
- 新トークンが別ペアの旧トークンを含むような病的な名前衝突は検出しない (置換カウントの検分で人間が気づける形にはなっている)
- **caption 置換は属性値の完全一致で TWB 全域に当たる**ため、旧 PDS 表示名と完全一致する caption を持つ無関係要素 (column / dashboard object / action 等) があれば巻き添え改名される (接続には無影響・表示のみ)。疑わしければリハーサル画像で確認する
