---
purpose: migration-plan がオーケストレーションで満たす設計原則 (決定台帳 / courier / progressive fill / Stop 1)
note: SKILL.md が要点、本ファイルが根拠と詳細。計画書に何を載せ何を載せないか、下流とどう連携するかの判断基準を規定する。スキーマ実体は plan-format.md
---

# Orchestration Model

## 目次

- 決定台帳 ≠ ファクトキャッシュ
- progressive fill
- courier パターン
- Stop 1 の定義と Stop 2 との境界
- 発動条件の根拠
- 非スコープ

## 決定台帳 ≠ ファクトキャッシュ

計画書に載る情報は 3 分類。**流用してよいのは決定だけ**。これは `tableau-prep-schedule-designer/references/scheduling-model.md` の「run-type を設計文書から転記するな」を計画書全体へ一般化したもの。

| 分類 | 定義 | 正本 | 計画書での扱い |
|---|---|---|---|
| **決定** | ユーザー判断由来で AI が再導出できない | **計画書が正** | 値を保持。後段が courier で流用 |
| **ファクト** | 機械確定できる (run-type / LUID / 依存エッジ / content_url) | `.tfl` 実体 / manifest / flow-dependencies.md | **ポインタのみ**。値をキャッシュしない。載せる場合も `未確認` ラベル + source path |
| **status** | 各工程の進捗 | manifest 群 / verify 出力 | **再導出キャッシュ**。resume で突合。正本にしない |

帰結: `migration-plan.json` は決定とポインタだけを実データとして持ち、ファクト値をキャッシュしない。これにより古くなるのは status キャッシュだけになり、それは manifest から再導出できる。実データ (特に run-type) を抱えると drift 事故 (append 出力の重複) に直結する。

## progressive fill

計画書は「冒頭で完成」ではなく、Workflow の各工程が該当セクションを埋めていく。

フィールドごとの必須 (init) / nullable (後段) の別は [plan-format.md](plan-format.md) を正とする。ここでは *なぜ* その配分になるかの根拠を述べる:

- **init で埋まる**のは、機械抽出できる (scope / order / backfill 候補) か intake で既に確定している (target / goal) もの。
- **後段に回す**のは、使う工程がずっと後で、かつその頃に判断材料が揃うもの (trigger_policy は probe 実測後、backfill mode は compare 後、repoint 対応は repoint 工程)。

**後ろ倒しが遅延収集アンチパターンにならない理由**: `trigger_policy` 等は使う工程 (schedule) がずっと後で、かつその頃には probe 実測という**より良い判断材料**が揃う。intake の「今聞けるのに後回しは往復が増える」に該当しない (今聞いても判断材料が無い)。

**matrix を decompose 後にする理由**: 行 = 分解後 .tfl 名が decompose の産物で init 時に存在せず、かつ init 時の status は全 pending で空格子に情報が無い。粒度自体が decompose を境に「元フロー単位 → 分解後 .tfl 単位」へ詳細化する。

## courier パターン

下流 Skill を計画書 artifact に**結合させない**。決定の受け渡しは常に main agent が担い、各 Skill の**既存の引数契約**に流し込む。

例 (schedule 工程):

1. main agent がユーザーにトリガ方針を聞く
2. `migration-plan.json` の `trigger_policy` を埋める
3. **同じ値を `tableau-prep-schedule-designer` の既存 `trigger_policy` 引数に渡す** (Skill は計画書の存在を知らない)
4. 完了後、該当 `human_queue` ステップの `runbook_ref` を埋め、matrix の schedule status を更新

同型を repoint (`manifest_paths` の集約先を `session_batches` から決める) / backfill (`backfill_candidates` を起動判断リストにする。Skill 内部の承認ゲートは別途走る) に適用する。

これにより各 Skill の standalone 起動性が保たれ (計画書なしでも起動可)、結合点は main agent 1 箇所に閉じる。

## Stop 1 の定義と Stop 2 との境界

Stop 1 = init 直後の**薄い**ユーザー確認。初版を提示して異論を受けるだけで、重い明示確認は持たない (scope は intake の追認、migration_order は機械導出の追認)。一気通貫を止めない。

| | Stop 1 (migration-plan) | Stop 2 (decomposition-plan) |
|---|---|---|
| 単位 | プロジェクト / バッチ | 1 フロー |
| 確認対象 | scope・順序・バッチ・横断工程適用・トリガ方針・人間作業段取り | 命名・レイヤ配置・Input policy・Output mapping |
| 正本 | 横断的決定 | フロー内設計 |
| 回数 | セッション/プロジェクトに 1 回 | フローごとに 1 回 |

Stop 1 が右列に触れないことが `tableau-prep-architect` との非結合を成立させている (`tableau-prep-architect` は `flow-dependencies.md` を直接消費する)。Stop 2 の観点は `tableau-prep-architect/references/review-checkpoints.md`。

## 発動条件の根拠

忘れ防止の対象 (スケジュール UI 作成・WB repoint の承認または Desktop 差し替え・backfill 承認) は移行完了後の**人間作業**で、**単発フローでも発生する**。したがって発動を「フロー数」だけで切ると単発 × 横断工程ありで台帳が失われる。正しい軸は 2 つ:

- **複数フロー** → migration_order / session_batches / scope の管理価値
- **Q2b (schedule / repoint / backfill) が非空** → human_queue の忘れ防止価値

schedule と repoint は独立に選べる (片方だけのケースがある)。横断工程は intake の Q2b で複数選択され `--crosscut` にそのまま渡る。「単発 × Q2b なし」だけが非作成ゾーン。

## 非スコープ

- **フロー内設計を持たない**: 命名・レイヤ・Input policy・Output mapping は `decomposition-plan-<flow>.json` が正本。
- **ファクトを配らない**: run-type / control field / LUID / content_url / 依存エッジは下流が実体から毎回再導出する。
- **status 再導出の自動化は初版スコープ外**: 手動更新で運用し、status が「古い正」になる兆候が出たら `manifest 群 → matrix status` の再導出を後追いで足す。
- **接続の書き換え・スケジュール作成はしない**: それぞれ `tableau-workbook-repointer` (repoint モードの自動手術が既定、fallback は人間の Replace Data Source) / `tableau-prep-schedule-designer` (人間の UI Linked Task) の領分。planner は段取りを台帳化するだけ。
