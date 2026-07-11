---
purpose: 生成済み .tfl 群を Tableau Cloud に publish する具体手順
fetched_at: 2026-05-17
note: 前提チェック、推奨 publish 順序（stg → int → marts → rpt）、connections / credentials の扱い、エラーハンドリングを規定
---

# publish-recipe

publish フェーズの具体手順。`prep-builder` が生成した .tfl 群を、目的のプロジェクトに publish するワークフロー。

## 前提チェック

publish に進む前に確認すべき項目：

1. **.tfl ファイルの存在** — 入力パスにファイルが実在するか
2. **命名規約適合** — ファイル名が `stg_` / `int_` / `fct_` / `dim_` / `rpt_` のいずれかで始まるか（[../../../../references/naming-conventions.md](../../../../references/naming-conventions.md)）
3. **親プロジェクトの存在** — `create_projects.py` で stg/int/marts サブプロジェクトが作成済みか
4. **OAuth サインインの成立** — ブラウザサインインが 5 分以内に完了するか（callback listener / ブラウザ起動可能な環境か）
5. **embed credentials の決定** — フローが生 DB 接続を持つか、仮想接続経由か

## 推奨 publish/run 順序 (レイヤ間は必ず順次)

依存順に **publish → run → dbname 解決 → 下流 .tfl patch → 次レイヤ publish** を回す：

```
1. stg_* を publish → run → finishCode=0 確認  (上流依存なし、並列 publish 可)
        ↓ stg レイヤの Published DS が Cloud 上に作成済み
2. auto_patch_downstream.py で manifest の ready PDS を全 .tfl に一括 patch
        ↓
3. int_* を publish → run → finishCode=0 確認
        ↓
4. auto_patch_downstream.py を再実行 (int も ready になる → 下流 .tfl が更新される)
        ↓
5. fct_* / dim_* を publish → run → finishCode=0 確認
        ↓
6. auto_patch_downstream.py を再実行 (fct/dim も ready に → rpt_* .tfl が更新される)
        ↓
7. rpt_* を publish → run → finishCode=0 確認
```

`auto_patch_downstream.py` は「Cloud 上に PDS が実在する」entry 全件を ready 集合として、全 .tfl をスキャン → 参照のある PDS の dbname を Cloud から resolve → 一括 patch する。ready の条件は kind で異なる: `kind=tfl` は `run.status == success` (run が PDS を実体化する)、`kind=pds_augment` は `publish.status == published` (Live PDS は publish 時点で実在、run は n/a)。idempotent (再実行しても同じ dbname なら no-op) なので、各レイヤ完走後に毎回呼んで良い。同一レイヤ内に sub-DAG がある (例: intermediate 内で int_price_latest → int_transactions_enriched) 場合も、sub-DAG の wave 完走ごとに呼べばカバーできる。手動で `discover_pds_dbname.py --patch` を 1 ペアずつ叩く必要は無くなった。

**stg が `kind=pds_augment` (Live PDS) を含む場合の順序**: Live PDS の実 dbname (content_url) は publish して初めて確定するので、**stg レイヤの publish → manifest update-publish → `auto_patch_downstream.py` → それから int を publish** の順を守る。patch 前に int を publish してしまうと、サーバー上の flow は placeholder dbname のまま run fail するため、patch 後に `--mode Overwrite` で再 publish が必要になる (手戻り)。stg augment は run を持たないので「stg の run 完了を待つ」ステップは無い。

**なぜ run まで挟むか**: 各レイヤの flow Input (LoadSqlProxy) は上流レイヤの **Published DS が Cloud 上に既に存在すること** を前提に publish される。run 前は publish 自体は通っても、上流 PDS が無い状態で run すると `Input data source not found` で finishCode=1。1 レイヤ完了 (publish + run + 成功確認) してから次レイヤに進む。

**append / incremental フローの run 規律 (元フローが incremental だった .tfl のみ)**: 出力が append モードの .tfl ([build-recipe §3d-3](../../prep-builder/references/build-recipe.md) の `set_incremental_refresh`) は run 種別に注意する。

- `run_flow.py` / `run_layer.py` の既定は **full run** (空 body の `/run`)。append 出力に full run を当てると**現スナップショットが毎回追記され出力が多重化する** (実際に #2 で 112→224 の事故を踏んだ)
- 正しい運用: **初回だけ full run で baseline を作り、以後は `run_flow.py --incremental`**。incremental run は control field の high-water mark を超える新規行のみ読んで append する
- **重複させてしまったら**: 出力 PDS を削除 → full run で 1 バッチ分を作り直し (LUID/dbname が変わるので下流 .tfl を `auto_patch_downstream.py` で再 patch) → 以後 incremental
- 本番スケジュールでは Tableau 側のスケジュール run-type を incremental に設定する (REST /run には runMode を毎回渡す必要があるが、スケジュールは設定で固定できる)

**dbname の publish/run 時挙動**:

- publish 時には `dbname` の **存在** が必須 (欠落で publish 拒否、対処は本ファイル末尾の対処表参照)。中身は妥当性チェックされない (placeholder 文字列で OK)
- run 時には **実 dbname が必要** (= 上流 PDS の物理 Hyper 名と一致しないと `Input data source not found` 系で finishCode=1)
- `flow_io.add_pds_input` は `dbname=None` 渡されたら `<datasourceName>_placeholder` を自動挿入するので publish は通る
- 上流 publish/run 完了後に [../scripts/auto_patch_downstream.py](../scripts/auto_patch_downstream.py) で実 dbname を一括 resolve → 全 .tfl の LoadSqlProxy + dataConnection 両方の dbname を書き換える。1 PDS だけ debug 等で patch したい場合は [../scripts/discover_pds_dbname.py](../scripts/discover_pds_dbname.py) を直接叩く。

**並列化できる粒度**: 同一レイヤ内の複数 .tfl は **publish のみ並列可**。**run は同一 PAT では直列が前提** — `--wait` を 2 プロセス並列で走らせると、**Tableau Cloud が同一 PAT で 1 active session のみ許可** する仕様により、後発 `run_flow.py` の sign_in が先発の token を server-side で revoke、先発の polling は 401 で死ぬ (TSC client 側の問題ではなく Tableau Cloud の認証仕様、検証済)。並列したい場合は [scripts/run_layer.py](../../../../scripts/run_layer.py) を使う — `run_flow.py --no-wait` を sequential 発火 → 単一 sign-in session で全 jobId を polling → manifest 更新まで一気通貫で行う。server-side では job が並列実行されるため wall-clock は `max(durations)` で済む。詳細と仕組みは [run-and-poll.md の §並列実行と排他](run-and-poll.md#並列実行と排他)。

**レイヤ間ゲートは承認ではなく依存関係**: 各レイヤ完走 (publish + run + finishCode=0) してから次レイヤへ進むが、これは下流 Input が上流 PDS を参照する依存性のためで、人間承認のためではない。途中レイヤで finishCode=1 や publish エラーが出たら [autonomous-recovery.md](autonomous-recovery.md) で分類 → 自律リトライ or escalation。escalation 発火時は下流レイヤに進まずユーザーに報告。

`rpt_*` は fct/dim の Published DS を Input として読むので必ず最後。スケジュール実行 (本番運用) では Tableau の Linked Tasks で fct/dim → rpt の連鎖を組む。

## publish 後の manifest 更新

各 .tfl の publish が成功 (HTTP 201) するたびに、戻り値の flow LUID を控えて [scripts/publish_manifest.py update-publish](../../../../scripts/publish_manifest.py) で session manifest を更新する:

```bash
python scripts/publish_manifest.py update-publish \
  --manifest <session>/reports/publish-manifest.json \
  --flow-name <decomposed_flow_name> \
  --status published \
  --flow-luid <luid>
```

publish が失敗 (HTTP 4xx/5xx でリトライ尽きた状態) なら:

```bash
python scripts/publish_manifest.py update-publish \
  --manifest <session>/reports/publish-manifest.json \
  --flow-name <decomposed_flow_name> \
  --status failed
```

`status=failed` の場合は `--flow-luid` 不要。manifest 形式は [../../../../references/publish-manifest-format.md](../../../../references/publish-manifest-format.md)。

## `publish_flow.py` の使い方

```bash
# プロジェクトパスで指定（推奨、可読性が高い）
python publish_flow.py \
  --file ./flows/staging/stg_salesforce__opportunities.tfl \
  --project-path "Sales Analytics/stg"

# プロジェクト ID で指定（曖昧さを避けたいとき）
python publish_flow.py \
  --file ./flows/marts/fct_sales.tflx \
  --project-id 12345-abcde

# 上書き publish
python publish_flow.py --file ... --project-path ... --mode Overwrite

# 名前をファイル名と変える
python publish_flow.py --file stg_orders.tfl --project-path "..." --name "stg_orders_v2"
```

スクリプトは常に非対話で動く (承認は session intake で済んでいる前提、[autonomous-execution-policy.md](autonomous-execution-policy.md) 参照)。

## `mode` の使い分け

| Mode | 挙動 | 使うとき |
|---|---|---|
| `CreateNew`（既定） | 同名の flow が既存なら 409 エラー | 初回 publish、新規追加 |
| `Overwrite` | 既存の flow を上書き | 修正版の再 publish |

⚠️ **`Overwrite` は前バージョンを潰す**（Tableau Cloud のバージョン履歴は保持されるが、UI から戻す手間が増える）。本番運用では：
- 大きな変更は `CreateNew` + 別名で先に動作確認
- 軽微な修正のみ `Overwrite`

## Embed Credentials の扱い

フローの入力／出力がどの種類かで、必要な追加情報が変わる：

| 入出力種別 | 追加で必要なもの |
|---|---|
| **仮想接続** ([input-policy](../../../../references/input-policy.md) 推奨) | **不要** — 仮想接続の認証は Tableau Server 側に組み込み済み |
| **Published Data Source** | **不要** — サインインユーザー（サービスアカウント）で読める権限があれば OK |
| 生 DB 接続 (例外的に残る場合) | `connections` パラメータで DB ユーザー名・パスワードを embed |
| ローカルファイル | ファイルが Tableau Server からアクセス可能なネットワーク共有上にあること |

`publish_flow.py` は **仮想接続 / Published DS 前提** で動く。生 DB 接続を持つ flow を publish するには `connections` 周りの実装拡張が必要（未対応）。

## バッチ publish（レイヤ単位で publish → run → 次レイヤ）

publish は 1 ファイル単位 (`publish_flow.py`)、run はレイヤ単位 (`run_layer.py`) で並列化する。レイヤ間は依存関係上、必ず順次:

```bash
MANIFEST=<session>/reports/publish-manifest.json
TARGET=<target>
FLOWS=<session>/flows
PATCH="python .claude/skills/prep-deployer/scripts/auto_patch_downstream.py \
  --manifest $MANIFEST --flows-dir $FLOWS --target-path $TARGET"

# Layer 1: stg
for f in flows/staging/*.tfl; do
  python publish_flow.py --file "$f" --project-path "$TARGET/stg"
done
python scripts/run_layer.py --manifest $MANIFEST --layer staging
$PATCH   # stg PDS が ready -> 下流 .tfl の stg ref を一括 patch
# ── exit code 0 を確認してから次へ ──

# Layer 2: int (stg の PDS を Input に取る)
for f in flows/intermediate/*.tfl; do
  python publish_flow.py --file "$f" --project-path "$TARGET/intermediate"
done
python scripts/run_layer.py --manifest $MANIFEST --layer intermediate
$PATCH   # int も ready -> 下流 .tfl の int ref も patch
# ── exit code 0 を確認してから次へ ──

# Layer 3: marts (fct/dim 先、rpt 最後)
for f in flows/marts/fct_*.tfl flows/marts/dim_*.tfl; do
  python publish_flow.py --file "$f" --project-path "$TARGET/marts"
done
python scripts/run_layer.py --manifest $MANIFEST --layer marts
$PATCH   # fct/dim ready -> rpt_*.tfl の ref を patch
for f in flows/marts/rpt_*.tfl; do
  python publish_flow.py --file "$f" --project-path "$TARGET/marts"
done
python scripts/run_layer.py --manifest $MANIFEST --layer marts
```

`run_layer.py` は manifest の対象レイヤから `publish=published` && `run!=success` の全件を拾うので、rpt の追加 publish 後の 2 回目呼び出しでは未 run の rpt のみが選択される。`auto_patch_downstream.py` は idempotent なので各レイヤ完走後に毎回呼んで OK (同じ dbname の re-patch は no-op)。

スクリプトは常に非対話で動き、各レイヤ完走を確認してから次レイヤへ進む (依存関係上のゲートであって承認ゲートではない、[autonomous-execution-policy.md](autonomous-execution-policy.md) 参照)。失敗時の自律対処は [autonomous-recovery.md](autonomous-recovery.md)。

## publish エラーの戻り先マップ

`publish_flow.py` が REST エラーを返してきたときの判定基準と戻り先:

| Tableau errorCode | symptom | 想定原因 | 戻り先 |
|---|---|---|---|
| `280003` ("Problem reading the provided Flow file") | publish HTTP 400 | (a) 生成 .tfl に `maestroMetadata` 等の aux entry が無い / (b) Input ノードに connection 登録なし (孤立 connectionId) / (c) **複数の重複 Tableau Server connection entry** (KB 005232681) / (d) LoadSqlProxy / dataConnection の `dbname` 欠落 / (e) LoadSqlProxy ノードに必須デフォルトフィールド (`relation`, `actions`, `debugModeRowLimit` 等) の欠落 | (a) `aux_entries=` 渡し忘れを確認 / (b) `flow_io.add_pds_input` で一括登録 / (c) `add_pds_input` は dedup するので自前生成を疑う / (d) `add_pds_input` は dbname=None 渡しても placeholder を自動挿入する / (e) `make_load_sql_proxy_node` のデフォルトに含まれている — 自前構築している場合は要追加 |
| 4xx `Input data source not found` 系 | publish/run | 上流レイヤの PDS が Cloud 上に存在しない | 上流レイヤの publish + run を先に完走させる ([レイヤ順次の節](#推奨-publishrun-順序-レイヤ間は必ず順次)) |
| 401 / 403 | publish | access token 失効 / サインインユーザーの権限不足 | [authentication.md](authentication.md) |
| 404 (project) | publish | preflight 未実施 / project 削除済 | prep-extractor Phase B → preflight 再実行 |
| 409 (name conflict) | publish | 同名 flow が CreateNew で既存 | `--mode Overwrite` 確認、または名前変更 |

実値 (上記以外のコード) を観測したら本表に追記して育てる。

## ロールバック

publish 失敗時 or publish 後に問題が見つかったときの戻し方：

| 状況 | 対処 |
|---|---|
| publish 自体が失敗（HTTP エラー） | エラーメッセージに従い修正・再 publish。Tableau Cloud 側に副作用なし |
| publish は成功したが flow が動かない | Tableau Cloud の **バージョン履歴** から前バージョンに戻す（UI: Flow → Revision History） |
| 複数 flow を publish 中に途中失敗 | 既に publish 済みのものを **手動で削除 or 戻す**。スクリプト側で自動ロールバックはしない |

失敗の分類と自律対処の詳細は [autonomous-recovery.md](autonomous-recovery.md)。ロールバックは引き続き自動化せず手動 (監査ログ保全のため、[autonomous-execution-policy.md](autonomous-execution-policy.md))。
