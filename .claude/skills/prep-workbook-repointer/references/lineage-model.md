---
purpose: prep-workbook-repointer が lineage をどう読み、旧→新 PDS 対応をどう機械確定し、verify で反映をどう突合するかの設計モデルと根拠
note: 手順そのものは SKILL.md、出力フォーマットは repoint-format.md。本ファイルは「なぜこの読み方か」の判断根拠 (誤 FAIL 回避・反映ラグ・スコープ境界) を集約する
---

# Lineage Model

`prep-workbook-repointer` が Metadata API lineage と publish-manifest を突合して
「どの Workbook を・どの接続を・どの新 PDS 名へ差し替えるか」を機械確定し、差し替え後に
反映を検証するための設計モデル。

## 目次

- 左辺: lineage の読み方 (downstreamWorkbooks のみ)
- 右辺: 旧→新 PDS の join キー
- 絞り込みをしない (対象 WB)
- webpage_url と content_url の役割
- verify: 反映突合と eventual consistency
- スコープ境界 (やらないこと)

## 左辺: lineage の読み方 (downstreamWorkbooks のみ)

「旧 PDS を参照する WB」の抽出には Metadata API GraphQL の
`publishedDatasources { downstreamWorkbooks { luid name projectName } }` **だけ** を使う
(`/api/metadata/graphql`、read-only)。TSC の `server.metadata.query()` 経由で叩く。

逆方向の `workbooks { upstreamDatasources }` は **使わない**。理由:

- Metadata API はオブジェクト型ごとに使えるフィールドが異なり、名前の揺れ
  (`upstreamDatasources` / `upstreamPublishedDatasources`) もある
- **未確認フィールドを仮定すると空応答が返り、それを「参照 WB なし」と誤読して偽 FAIL / 偽 empty を生む**

`downstreamWorkbooks` は実データを返すことが確認済みで、design (棚卸し) と verify (反映突合) の
両方をこの 1 方向のクエリだけで完結させる。GraphQL が `errors` を返した場合は空結果でも
**errors を必ず前面に出す** — 「空 = 影響 WB なし」と誤断させないため。

## 右辺: 旧→新 PDS の join キー

新 PDS への対応は publish-manifest ([../../../../references/publish-manifest-format.md](../../../../references/publish-manifest-format.md)) から引く。
manifest の `decomposed_flows[].source_original_output_name` が **旧 output PDS 名 → 分解後フロー**
の 1:1 リンク (直感と逆の対応も機械確定される)。join の連鎖:

1. inventory の旧 PDS `luid` を manifest の `original.outputs[].luid` と照合 → 旧 output `name` を得る
2. 同 manifest の `decomposed_flows[]` で `source_original_output_name == 旧 output name` を探す
   → その `outputs[0]` が **新 PDS** (name / luid)

**主キーは luid**。ただし manifest の `original.outputs[].luid` が null (resolve-luids 未実行) の場合は
**PDS 名での fallback join** に切り替え、その旨を warning に立てる。luid 一致が取れないまま name で
救済したペアは design.json の `match: "name"` で明示する (人間が対応の妥当性を確認できるように)。

manifest に対応が無い旧 PDS (WB から参照されているが `source_original_output_name` で拾えない) は
`unmapped_old_pds` に落とす。移行対象外か、manifest の渡し漏れ / resolve-luids 未実行のサイン。

## 絞り込みをしない (対象 WB)

対象 WB は **利用状況で絞り込まない**。デモ / 拡張系 (例: `99_Extensions` 配下) も含め、
旧 PDS を参照する WB を全件掲載する。利用状況は本 Skill から判定できず、取捨は人間判断だから。
各 WB には `webpage_url` を付し、人間がすぐ開いて要否を判断できるようにする。

owner 等の**個人情報 (メールアドレス含む) は成果物に出さない**。lineage から owner を取得できても
runbook / design.json には載せない。

## webpage_url と content_url の役割

- **webpage_url** (WB): Desktop で対象 WB を開くための URL。TSC `WorkbookItem.webpage_url` から解決。
  runbook の主役の一つ (もう一つは新 PDS *名*)。
- **content_url** (新 PDS): TSC `DatasourceItem.content_url` から解決し design.json に残す。
  Desktop の Replace Data Source (名前で選ぶ) では使わないが、**repoint モードの TWB 手術が
  置換キーとして消費する** ([twb-surgery.md](twb-surgery.md))。解決できなくても design は成立する
  (best-effort — repoint モードが手術時に LUID から再解決する)。

REST workbook connections が返す `datasource id` は **PDS 本体の LUID ではない** (shadow id)。
接続の即時チェックは `datasource_name` で行い、LUID 級の裏取りは Metadata API lineage で取る。

## verify: 反映突合と eventual consistency

verify は design と **同じ `downstreamWorkbooks` クエリを 1 回だけ** 実行し、design.json の各
(旧 PDS → 新 PDS, WB) について 2 方向を確認する:

- 旧 PDS の downstreamWorkbooks から当該 WB が **消えた** か (`old_removed`)
- 新 PDS の downstreamWorkbooks に当該 WB が **現れた** か (`new_present`)

判定:

| verdict | 条件 |
|---|---|
| `reflected` | old_removed かつ new_present |
| `partial` | どちらか片方のみ |
| `not_reflected` | どちらも未 |

overall は全 WB が `reflected` のときだけ `PASS`、それ以外は `INCOMPLETE`。

**反映ラグ (eventual consistency)**: Metadata API lineage は republish 後に反映ラグがある
(migration の resolve-luids が "No flow found" で数回リトライしたのと同型)。差し替え直後の verify は
未反映で `not_reflected` になりうる。したがって verify は:

- **単一スナップショット**を取って報告するだけ (内部リトライループは持たない — fork 内で放置しない)
- レポートで「未反映は時間をおいて再実行」を案内する (反映は republish 後数分以内に完了することが
  多いが保証はない)
- **fail を自分では直さない**。数回再実行しても解消しないときだけ、差し替え (repoint モードまたは
  Desktop) をやり直すか、caller が design を再実行する

## スコープ境界 (やらないこと)

| 関心事 | 担当 |
|---|---|
| 接続の実書き換え | 本 Skill の **repoint モード** (TWB 手術 + republish、[twb-surgery.md](twb-surgery.md)) が既定。fallback = 人間 (Desktop の Replace Data Source) |
| 列等価性 / 壊れるビュー予告 (field-parity) | prep-output-comparator |
| 旧 flow スケジュール停止 | prep-schedule-designer |
| 旧 PDS の残置 / 削除判断 | 人間 (migration の step 判断) |
| 薄い行数 (baseline-forward) の gating | 持たない (migration 側の関心事) |

design / verify モードは read-only。サーバー書き込みは repoint モードの WB republish のみ。
