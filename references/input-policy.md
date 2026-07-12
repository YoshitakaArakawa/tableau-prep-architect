---
purpose: Tableau Prep の Input ノードを Published Data Source / 仮想接続経由に統一する原則
note: 違反判定の例、各 Skill での扱い（analyze での違反検出 / decompose での置換 / publish での credentials 扱い）を規定
---

# input-policy

Tableau Prep フローの **Input ノードは Published Data Source または仮想接続を指す** ことを原則とする。例外: 一時的な CSV/Excel（PoC・スクラッチ用途）。

## 理由

- スキーマ変化を仮想接続層で吸収できる
- Metadata API で lineage を取得しやすい
- 接続情報を集中管理できる（資格情報の散在を防ぐ）

dbt の `sources` 概念に相当。

## 各 Skill での扱い

- **prep-architect / analyze**: 違反（生 DB 直結、ローカルファイル直読）を `Input Compliance` セクションで列挙し、仮想接続化の migration suggestion を出す
- **prep-architect / decompose**: 違反 Input は新 .tfl の Input 設計で仮想接続経由に置き換える
- **prep-deployer / publish**: 仮想接続 / Published DS 経由なら embed credentials 不要、生 DB 接続なら `connections` パラメータ拡張が必要

## stg を Live PDS で表現する場合の束縛層制約

下流 Prep flow (LoadSqlProxy) は PDS のフィールドを **local-name** で束縛する (caption は BI / VizQL 表示専用)。stg を prep-pds-augmenter の Live PDS で表現し、その stg を下流 Prep が読む構成では:

- **rename**: vconn source の true rename (local-name 書き換え + `<cols><map>`) でのみ成立。caption-only rename の stg は下流 run が "Unknown field name" で fail する。semantics の詳細は prep-pds-augmenter SKILL.md 参照
- **cast / hide**: 下流 Prep から見た挙動は **未検証**。Prep 消費前提の stg にこれらの op を含める場合は Stop 2 で未検証リスクとして明示し、検証を挟むか実 .tfl 化を検討する

## 命名レジーム: 元の内部名を end-to-end 保持 (正典)

分解後の各層が公開する列名は **元フローの内部名 (post-Input 命名。actions-split で stg に吸収した正規化 rename を含む) をそのまま保持** する。stg / int の列名を英語等へ意訳する semantic translation は **行わない**。

理由は build の転写モデルと束縛層の掛け算:

1. prep-builder (`build_from_plan.py`) は下流ノードを **式ごと verbatim 転写し、列参照を書き換えない**。転写式は列を元の内部名で参照するため、上流 PDS が別名 (英語等) を公開すると下流 run が "Unknown field name" で fail する
2. ある層の英語化は「下流式の全 rewrite」(AddColumn 式 / Filter 式 / LOD の PARTITION・ORDERBY / Join clause / サフィックス派生列) を要求し、決定論的 verbatim 転写というパイプラインの設計意図と両立しない。列参照 rewriter は実装しない (非採用)
3. 「caption だけ英語・内部名は元のまま」の折衷も **非成立** (検証済み): 上記のとおり下流 Prep は local-name で束縛し caption を見ないため、下流から英語名は見えない

この regime では: 元 output を引き継ぐ mart の列名 parity は **自動達成** され、Rename-back は上流で divergent な forward rename を導入した場合 (原則発生しない) のみ必要になる。BI 向けの表示名変更は mart 境界より下流 (PDS の caption / Workbook 側) で行う。

## 違反の判定例

| Input 種別 | nodeType | 判定 |
|---|---|---|
| Published Data Source 経由 | `LoadSqlProxy` | ✅ Compliant |
| 仮想接続経由 | `LoadSql` で `connectionType=tableau-server` 等 | ✅ Compliant |
| 生 DB 接続（Snowflake / Postgres など直結） | `LoadSql` で具体的な DB host を指す connection | ❌ 違反、仮想接続化を提案 |
| ローカル CSV / Excel | `LoadCsv` / `LoadExcel` | △ PoC は許容、本番では DB 経由を提案 |
| Hyper（中間生成） | `LoadHyper` | 本リポでは cross-layer 連鎖に **使わない** (全層 Published DS publish が前提、cross-layer Input は `LoadSqlProxy` 経由で上流レイヤの PDS を読む)。Prep Builder GUI での単体検証用のローカル Hyper のみが対象 |
