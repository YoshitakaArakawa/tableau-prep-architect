---
purpose: Tableau Prep の Input ノードを Published Data Source / 仮想接続経由に統一する原則
fetched_at: 2026-05-17
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

## 違反の判定例

| Input 種別 | nodeType | 判定 |
|---|---|---|
| Published Data Source 経由 | `LoadSqlProxy` | ✅ Compliant |
| 仮想接続経由 | `LoadSql` で `connectionType=tableau-server` 等 | ✅ Compliant |
| 生 DB 接続（Snowflake / Postgres など直結） | `LoadSql` で具体的な DB host を指す connection | ❌ 違反、仮想接続化を提案 |
| ローカル CSV / Excel | `LoadCsv` / `LoadExcel` | △ PoC は許容、本番では DB 経由を提案 |
| Hyper（中間生成） | `LoadHyper` | レイヤ連鎖の中間入力、本ポリシーの対象外 |
