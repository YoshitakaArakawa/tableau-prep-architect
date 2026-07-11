---
name: prep-deployer
description: prep-builder が生成した .tfl 群を Tableau Server/Cloud に preflight・publish・run する。session intake で goal と target path が合意された前提で承認プロンプトなしに自律実行し、失敗は autonomous-recovery の分類で自動リトライ、回復不能種別 (認証 / 権限 / 容量 / Cloud 障害) は escalation する。.tfl 群が手元に揃っていてサーバーに届けたいとき、publish 済み flow を実行したいとき、ジョブ結果を確認したいとき、「デプロイして」「publish して」「実行して」と言われたときに起動。
---

# prep-deployer

prep-builder が組み立てた .tfl 群を Tableau Server/Cloud に届け、運用副作用 (preflight / publish / run) を扱う Skill。**session intake (CLAUDE.md step 0) で goal (Q2) と target path (Q4) が合意済みの前提で、書き込み操作は承認プロンプトを出さずに自律実行する**。失敗は [autonomous-recovery](references/autonomous-recovery.md) のマッピングで分類し、回復可能なら自動リトライ、回復不能なら escalation。

本 Skill は `context: fork` を **付けない**。理由は publish / run の失敗を主会話で観測し、recovery ループの最終 escalation を主会話に報告する必要があるため。

役割分担: **読み取り = prep-extractor** (Phase B: Cloud structure extraction → `deploy-context.md`)、**書き込み = prep-deployer**。preflight 以降の各フェーズは `deploy-context.md` を入力として消費する (無ければ prep-extractor の Phase B を先に呼ぶよう案内)。

build フェーズは別 Skill ([prep-builder](../prep-builder/SKILL.md))。publish 失敗で .tfl 修正が必要なときは prep-builder に戻る。

## Phases

### Preflight

[prep-extractor](../prep-extractor/SKILL.md) の Phase B が生成した `deploy-context.md` を読み、**target までの pending セグメント** (任意深さ、N 個) と **dbt 3 レイヤ** を一括で作成する。

| 項目 | 内容 |
|---|---|
| 入力 | `deploy-context.md` (prep-extractor Phase B 出力) |
| 出力 | pending セグメントと dbt サブプロジェクトの作成結果 (idempotent) |
| 副作用 | サーバー副作用あり (プロジェクト作成のみ、データには触れない) |
| 承認 | session intake の target path 指定で合意済み。追加プロンプトなし |

アルゴリズム・中断時の挙動は [references/preflight-recipe.md](references/preflight-recipe.md) を参照。スクリプトは `create_project.py` (1 セグメント) と `create_projects.py` (dbt 3 レイヤ一括)、いずれも非対話で動く。

### Publish

prep-builder が生成した成果物 (`flows/<layer>/*.tfl` および `flows/staging/*.augmenter.json`) を、Tableau Server/Cloud 上の dbt 風プロジェクト階層 ([../../../references/project-hierarchy.md](../../../references/project-hierarchy.md)) に publish する。Preflight が先に走り、サブプロジェクトが揃っている前提。

| 項目 | 内容 |
|---|---|
| 入力 | `flows/{staging,intermediate,marts}/*.tfl` (kind=tfl) + `flows/staging/*.augmenter.json` (kind=pds_augment) + `deploy-context.md` (target LUID 取得用) |
| 出力 | Tableau Cloud 上に publish 済み Flow / Live PDS |
| 副作用 | サーバー副作用あり |
| 承認 | session intake の goal=④ 指定で合意済み。失敗時は [autonomous-recovery](references/autonomous-recovery.md) で分類 → 自律リトライ or escalation |

手順:
1. `deploy-context.md` で **dbt layer presence が全て yes** であることを確認 (不足があれば Preflight に戻る)
2. `publish-manifest.json` を読み、各 decomposed entry を **kind dispatch**:
   - `kind=tfl` → `scripts/publish_flow.py` でレイヤ対応プロジェクトに .tfl を publish (embed credentials は必要に応じて)
   - `kind=pds_augment` → `python .claude/skills/prep-pds-augmenter/scripts/augment_pds.py --spec <augmenter_spec_path> --out-dir <session>/augmenter_out/<name>/` を呼ぶ。`RESULT_JSON` を parse して `published_luid` を取得
3. publish 1 件ごとに [scripts/publish_manifest.py update-publish](../../../scripts/publish_manifest.py) で manifest を更新:
   - `kind=tfl`: `--flow-luid <luid>` を渡す
   - `kind=pds_augment`: `--pds-luid <luid>` を渡す (manifest 側は自動的に `outputs[0].luid` にもミラー)
4. HTTP / augmenter エラーが出たら errorCode を [autonomous-recovery.md の publish 表](references/autonomous-recovery.md) で分類

詳細手順: [references/publish-recipe.md](references/publish-recipe.md)
プロジェクト階層: [../../../references/project-hierarchy.md](../../../references/project-hierarchy.md)
スクリプト: `scripts/create_projects.py`, `scripts/publish_flow.py`, [.claude/skills/prep-pds-augmenter/scripts/augment_pds.py](../prep-pds-augmenter/scripts/augment_pds.py), [../../../scripts/publish_manifest.py](../../../scripts/publish_manifest.py)

### Run

publish 済みの flow を Tableau Server/Cloud 上で実行する。**`kind=pds_augment` の entry は Live PDS なので run フェーズを skip** (manifest 上は `run.status=n/a` のまま)。

| 項目 | 内容 |
|---|---|
| 入力 | flow ID または name + project (kind=tfl のみ対象) |
| 出力 | ジョブ完了レポート (finishCode 0/1/2 + `RESULT_JSON: {...}` 行) |
| 副作用 | サーバー副作用あり (production data 書き換え) |
| 承認 | session intake の goal=④ 指定で合意済み。finishCode=1/2 時は [autonomous-recovery](references/autonomous-recovery.md) で分類 |

REST API:
- `POST /api/3.x/sites/{site-id}/flows/{flow-id}/run` → ジョブ開始、jobId を取得
- `GET /api/3.x/sites/{site-id}/jobs/{job-id}` → ステータス取得
- `finishCode`: 0 = Success, 1 = Failed, 2 = Cancelled

`run_flow.py` は wait モードがデフォルトで、終了時に `RESULT_JSON: {jobId, finishCode, notes, durationSec, ...}` を 1 行出力する。AI Agent はこれを parse して recovery ループの次アクションを決める。

run 1 件ごとに [scripts/publish_manifest.py update-run](../../../scripts/publish_manifest.py) で `publish-manifest.json` を更新 (finishCode を記録)。全レイヤ完走後に [scripts/publish_manifest.py resolve-luids](../../../scripts/publish_manifest.py) を 1 回呼んで、元フローの LUID + 全 PDS LUID を Metadata API で解決する。後段の prep-output-comparator が消費する。

詳細手順: [references/run-and-poll.md](references/run-and-poll.md)
失敗時の自律回復: [references/autonomous-recovery.md](references/autonomous-recovery.md)
スクリプト: `scripts/run_flow.py`, `scripts/get_job_status.py`, [../../../scripts/publish_manifest.py](../../../scripts/publish_manifest.py)

## 失敗時の戻り先

失敗の分類表・修正アクション・リトライ上限・loop 検知・escalation 境界は [references/autonomous-recovery.md](references/autonomous-recovery.md) に集約。方向感: .tfl 自体の不備 (280003 等) は prep-builder に戻る、Cloud 構造の不整合 (404 project 等) は prep-extractor Phase B に戻る、認証 / 権限 / 容量 / Cloud 障害は escalation。

## How to invoke

| 指示 | 動作 |
|---|---|
| 「publish 先を確認して」「環境チェック」 | `deploy-context.md` が無ければ prep-extractor Phase B 起動を案内 → Preflight |
| 「publish して」 | `deploy-context.md` で前提確認 → 不足あれば Preflight → Publish → recovery ループ |
| 「実行して」「動かして」 | Run 実行 (publish 済み前提) → recovery ループ |
| 「デプロイして」「本番に出して」 | Preflight → Publish → Run を一気通貫、失敗時は autonomous-recovery で自律対処 |

書き込み副作用は session intake で合意済みのため、各フェーズ開始時の追加プロンプトは出さない。escalation 発火時のみ主会話に報告する。

## 認証

`.env` の `SERVER` / `SITE_NAME` を読み、OAuth 2.0 (Authorization Code + PKCE) のブラウザサインインで access_token を取得する。Repo 直下 [scripts/tableau_auth.py](../../../scripts/tableau_auth.py) の `signed_in_server()` context manager を import。.env の項目と運用の詳細は [references/authentication.md](references/authentication.md)。

## 実行ポリシー (最重要)

1. **承認は session intake で取り切る** — Q2 (goal) と Q4 (target path) が合意された時点で publish / run を一気通貫で実行
2. **失敗は自律ループで回復を試みる** — [autonomous-recovery](references/autonomous-recovery.md) の symptom→fix マッピングで分類、リトライ上限内で再試行
3. **回復不能種別は即 escalation** — 認証 / 権限 / 容量 / Cloud 障害 / loop 検知発火は AI では直せないので主会話に報告
4. **自動ロールバックはしない** — Tableau Cloud のバージョン履歴から手動で戻す方が監査ログがクリーン
5. **エラーは握り潰さない** — finishCode / notes / errorCode をそのまま会話に返す

詳細: [references/autonomous-recovery.md](references/autonomous-recovery.md)

## Scripts

| スクリプト | 役割 |
|---|---|
| `scripts/create_project.py` | 1 個のプロジェクトを既存の親の下に idempotent 作成 (preflight の pending loop 用、非対話、top-level は WARNING) |
| `scripts/create_projects.py` | target 配下に dbt 3 レイヤ (stg/int/marts) を idempotent 作成 (preflight 最終ステップ) |
| `scripts/publish_flow.py` | flow を指定プロジェクトに publish (非対話) |
| `scripts/run_flow.py` | flow 実行 (非対話、`--wait` デフォルト True、終了時に `RESULT_JSON: {...}` 行を emit) |
| `scripts/get_job_status.py` | ジョブステータス取得 |
| `scripts/discover_pds_dbname.py` | 1 PDS の物理 dbname を Cloud から resolve (debug / 1 件 patch 用、複数件まとめては `auto_patch_downstream.py`) |
| `scripts/auto_patch_downstream.py` | manifest の `run.status=success` 全件を ready 集合に、全 .tfl の LoadSqlProxy を一括 patch (idempotent) |

Cloud 側の **構造読み取り** (`deploy-context.md` 生成) は [prep-extractor の `get_project_structure.py`](../prep-extractor/scripts/get_project_structure.py) を使う (本 Skill では読み取り系スクリプトを持たない)。

同レイヤ並列 run の orchestration は repo 直下 [scripts/run_layer.py](../../../scripts/run_layer.py) (manifest 駆動、server-side parallel)。認証は全スクリプトとも Repo 直下 [scripts/tableau_auth.py](../../../scripts/tableau_auth.py) を import。

## 設計原則

実行ポリシー (上節) に加えて:

- 認証情報は `.env` 経由 (コミット禁止、`.gitignore` 済み)
- jobs.get / projects.get の結果はキャッシュせず、毎回サーバーから取得
- スクリプトは単独で動くようにする (Skill 経由でも、ユーザーがコマンドラインから直接呼んでも)
