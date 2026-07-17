---
purpose: 生成済み .tfl 群を Tableau Cloud に publish する具体手順
note: 前提チェック、推奨 publish 順序（stg → int → marts → rpt）、connections / credentials の扱い、manifest 更新を規定。失敗分類は autonomous-recovery.md に委譲
---

# publish-recipe

publish フェーズの具体手順。`prep-builder` が生成した .tfl 群を、目的のプロジェクトに publish するワークフロー。

## 目次

- 前提チェック / 推奨 publish/run 順序 (レイヤ間は必ず順次)
- publish 後の manifest 更新 / `publish_flow.py` の使い方 / `mode` の使い分け
- Embed Credentials の扱い / バッチ publish / publish エラーの扱い / ロールバック

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

**append / incremental フローの run 規律 (元フローが incremental だった .tfl のみ)**: 出力が append モードの .tfl ([special-outputs-recipe.md](../../prep-builder/references/special-outputs-recipe.md) の `set_incremental_refresh`) は run 種別に注意する。

- `run_flow.py` / `run_layer.py` の既定は **full run** (空 body の `/run`)。append 出力に full run を当てると**現スナップショットが毎回追記され出力が多重化する**
- 正しい運用: **初回だけ full run で baseline を作り、以後は `run_flow.py --incremental`**。incremental run は control field の high-water mark を超える新規行のみ読んで append する
- **重複させてしまったら**: 出力 PDS を削除 → full run で 1 バッチ分を作り直し (LUID/dbname が変わるので下流 .tfl を `auto_patch_downstream.py` で再 patch) → 以後 incremental
- 本番スケジュールでは Tableau 側のスケジュール run-type を incremental に設定する (REST /run には runMode を毎回渡す必要があるが、スケジュールは設定で固定できる)

**dbname の publish/run 時挙動**:

- publish 時には `dbname` の **存在** が必須 (欠落で publish 拒否 = 280003、[autonomous-recovery.md](autonomous-recovery.md))。中身は妥当性チェックされない (placeholder 文字列で OK)
- run 時には **実 dbname が必要** (= 上流 PDS の物理 Hyper 名と一致しないと `Input data source not found` 系で finishCode=1)
- `flow_io.add_pds_input` は `dbname=None` 渡されたら `<datasourceName>_placeholder` を自動挿入するので publish は通る。実 dbname への patch は上記 `auto_patch_downstream.py` (1 件だけなら [../scripts/discover_pds_dbname.py](../scripts/discover_pds_dbname.py))

**並列化できる粒度**: 同一レイヤ内の複数 .tfl は **publish のみ並列可**。run の並列は同一 OAuth セッションの排他制約があるため [scripts/run_layer.py](../../../../scripts/run_layer.py) 経由で行う (単一 sign-in session で `--no-wait` 発火 → 全 jobId polling、server-side では並列実行)。制約の仕組みは [run-and-poll.md の §並列実行と排他](run-and-poll.md#並列実行と排他)。

**レイヤ間ゲートは承認ではなく依存関係** (下流 Input が上流 PDS を参照するため)。途中レイヤで失敗したら [autonomous-recovery.md](autonomous-recovery.md) で分類、escalation 発火時は下流レイヤに進まない。

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

スクリプトは常に非対話で動く (承認は session intake で済んでいる前提、[autonomous-recovery.md §実行ポリシー](autonomous-recovery.md) 参照)。

## `mode` の使い分け

| Mode | 挙動 | 使うとき |
|---|---|---|
| `CreateNew`（既定） | 同名の flow が既存なら 409 エラー | 初回 publish、新規追加 |
| `Overwrite` | 既存の flow を上書き | 修正版の再 publish |

⚠️ **`Overwrite` は前バージョンを潰す**（Tableau Cloud のバージョン履歴は保持されるが、UI から戻す手間が増える）。本番運用では：
- 大きな変更は `CreateNew` + 別名で先に動作確認
- 軽微な修正のみ `Overwrite`

⚠️ **Overwrite の同一性判定は「名前 + プロジェクト」**。project にはその flow 自身が属するプロジェクトを指定する（output PDS のプロジェクト等を流用しない）。project を誤ると Overwrite にならず別プロジェクトに重複作成される。名前 + プロジェクトが一致した Overwrite は LUID を保持し、下流参照・スケジュール参照は無傷。

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

スクリプトは常に非対話で動き、各レイヤ完走を確認してから次レイヤへ進む。失敗時の自律対処は [autonomous-recovery.md](autonomous-recovery.md)。

## publish エラーの扱い

`publish_flow.py` が REST エラーを返したら、errorCode を [autonomous-recovery.md の Publish 失敗分類表](autonomous-recovery.md) で分類して対処する (280003 の sub-cause 別対処もそちらに集約)。未知のコードを観測したら recovery 側の表に追記して育てる。

## ロールバック

ロールバック方針の正典は [autonomous-recovery.md §ロールバック方針](autonomous-recovery.md)。要点: **AI は自動ロールバックしない** (監査ログ保全)。publish 自体の失敗は Cloud 側に副作用なし — エラーに従い修正・再 publish。publish は通ったが flow が動かない / 複数 flow の publish が途中で中途状態になった場合は、元 flow を残したままエラー報告し、必要なら人間が Cloud UI (Flow → Revision History) から revert / 既 publish 分の削除を判断する。
