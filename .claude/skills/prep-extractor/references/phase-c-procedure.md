---
purpose: prep-extractor Phase C (input-dispatch) の詳細手順。dispatch_inputs.py の出力 JSON を入力に、各 Input ノードへの取扱方針 (passthrough / augment / block) と policy 級 Transforms 提案を含む input-dispatch.md を生成する
fetched_at: 2026-05-24
note: Phase A/B と異なり LLM の semantic 判断 (caption の semantic translation, 提案文章) を fork 内で行う。mechanical な classify / LUID 解決はスクリプト側
---

# phase-c-procedure

Phase C は **分解元 Prep フローの各 Input ノードについて、後段 architect / builder がどう扱うかをユーザーと事前合意する** ためのフェーズ。Phase A/B 完了後・architect の analyze より前に実行。

## なぜ Phase C が必要か

- 入力種別 (`kind`: pds / vconn / direct_db / extract) は flow.json から決定論的に取れるが、**「整形済 PDS だから passthrough」「raw vconn だから augment」** といった取扱方針は業務判断であり auto-detect 禁止 (memory: `feedback_no_auto_detect_business_params` の系)
- direct_db Input は本 repo ではサポート外。**Prep に認証情報を埋め込まない方針** (Tableau Cloud 側で仮想接続 or PDS 化を先にしてもらう) を促す escalation 出口が必要
- caption の snake_case 化など列名の semantic 提案は AI 仕事で、ユーザーが行単位で受け入れ/拒否できる粒度で提示する必要

## フロー全体での位置

```
[Phase A]  flow extraction          → flow-summary.md
   ↓
[Phase B]  cloud structure          → deploy-context.md
            (target_path + 後述の Input PDS 親プロジェクトを --also-scan で含める)
   ↓
[Phase C]  input-dispatch (本フェーズ) → input-dispatch.md
            (proposal を書いてメインエージェント経由でユーザー確認 → 確定版を上書き保存)
   ↓
[architect analyze / decompose]    decomposition-plan-<flow>.md
```

Phase B を Phase C より先に走らせる必要があるが、**Phase B の `--also-scan` 引数に渡す追加スキャン対象 (= Input PDS の親プロジェクト群) は flow.json を読まないと判らない**。実用的な順序:

1. Phase A 実行 (flow.json + flow-summary.md を作る)
2. Phase B-pre 実行 (target_path のみで一度走らせる、または flow.json から先に親プロジェクトを集めておく)
3. dispatch_inputs.py を flow.json + Phase B-pre deploy-context.md で走らせる → `pds_project_parents_needed_in_scope` を確認
4. 親プロジェクトが target_path 配下に無ければ Phase B-rescan: `--also-scan <parent>` 付きで deploy-context.md を更新
5. dispatch_inputs.py を再実行して PDS LUID を確定
6. LLM が proposal markdown を書く

> ⚠️ 4-5 をスキップすると PDS LUID が `unresolved` のまま LLM が proposal を書くハメになる。passthrough を提案する PDS 行は LUID 解決前に proposal 提示しても良いが、ユーザー確認時に「未解決のままで OK か / Phase B 再 scan するか」を明示すること。

## スクリプトの責務 (mechanical)

`scripts/dispatch_inputs.py` の出力 JSON が LLM 入力。スクリプトが処理する範囲:

| 項目 | 内容 |
|---|---|
| Input 分類 | `flow_io.inspect_input_node` で `pds / vconn / direct_db / extract / unknown` に振り分け |
| PDS LUID 解決 | deploy-context.md の datasource 表をパース、(`projectName`, `datasourceName`) で照合。1 件一致 → `resolved`、複数 → `ambiguous` (candidates 列挙)、0 件 → `unresolved` (Phase B 再 scan 案内付き) |
| vconn metadata 抽出 | `resourceId` (vconn LUID) / `resourceName` / `relation.table` の bracket parse (table_uuid + table_name) / `fields[]` 一覧 |
| direct_db 情報 | base connection の `class` (snowflake / postgres / etc.) を抽出して block 理由として返す |
| fields 整理 | `isGenerated=True` を除外、`name_raw` / `name_bracketed` / `caption` / `datatype` を 1 列 1 オブジェクトに揃える |
| 追加スキャン要請 | flow.json 内の全 PDS Input の `projectName` 集合を `pds_project_parents_needed_in_scope` として emit |

スクリプト出力は **JSON 1 ファイル + stdout に `RESULT_JSON:` 行**。LLM はこの JSON を Read で取得して proposal markdown を起こす。

## LLM の責務 (semantic)

dispatch_inputs.py 出力を読んだ後、fork 内 LLM が以下を行う:

### 1. 各 Input への policy 提案

| kind | デフォルト提案 | 例外シグナル |
|---|---|---|
| `pds` | **passthrough** (PDS そのものを下流から直接参照、新規 stg 作成なし) | PDS 名から raw データの匂いがする (`*_raw`, `landing_*`, etc.) → **augment** を提案して理由を書く |
| `vconn` | **augment** (`kind=vconn` で stg PDS 新規 publish) | 単一テーブルで列もそのまま使う想定 → **passthrough** は不可 (vconn は PDS と違って直接下流参照できない構造ではないが、本 repo の方針として常に augment 経由で stg PDS を作る) |
| `direct_db` | **block** (escalation、Prep に認証情報を入れない方針を案内) | 例外なし |
| `extract` | **block** (escalation、cross-flow 共有不可な local extract のため) | 例外なし |
| `unknown` | **block** (LLM では判定不能なので人間判断要請) | 例外なし |

block 時のガイダンス文 (direct_db 用、autopilot しないユーザー方針に揃える):

> ### Direct DB Input は本ワークフローではサポートされません
>
> Prep flow に DB の認証情報を埋め込む運用を避ける方針のため、以下のいずれかを先に Tableau Cloud で行ってください:
>
> **推奨案 A: 仮想接続 (Virtual Connection) を作成**
> 1. Tableau Cloud で **新規 → 仮想接続** を作成
> 2. 対象 DB に接続、テーブルを選択して publish
> 3. Prep flow の Input を仮想接続経由に差し替えて再 extract
>
> **推奨案 B: extract → Published Data Source として publish**
> 1. Tableau Desktop で対象テーブルから .tdsx を作成
> 2. Tableau Cloud に publish
> 3. Prep flow の Input を PDS 参照に差し替えて再 extract
>
> どちらの案も本セッションを一旦終了して Cloud 側の整備を完了させてから、新しい flow を再 extract して Phase A から再開してください。

### 2. augment 行への policy 級 Transforms 提案

augment を選んだ Input については、`fields[]` を見て **policy 級 (2-3 行) の Transforms 提案** を組み立てる。列 UUID は出さない、操作カテゴリと対象列数で要約。

ルール:

- **rename**: ASCII caption (例: `Update Date`) は機械的に snake_case 化 (`update_date`)。**非 ASCII caption (日本語等) は semantic translation を提案** (`数量` → `quantity` / `単価 (Usd)` → `unit_price_usd`)。翻訳は LLM が文脈推測して提示、ユーザーが受け入れ/上書きする
- **cast**: 数値列 (`integer` → `real` 等) の cast は **業務文脈で判断する** ため AI 側からは強い提案しない。string → numeric のような型修正のみ提案 (例: 「`[amount_str]` (string) が数値表記なので integer に cast を推奨」)
- **hide**: 完全に未使用と判明している列のみ提案 (= flow-summary.md の Topology を見て下流参照ゼロの列のみ)。判定不能なら hide 提案を空にする

例 (Transactions vconn、8 列):

```
- 全 8 列を semantic translation + snake_case 化:
  取引→transaction_kind / 約定日→trade_date / 銘柄→ticker / 数量→quantity
  単価 (Usd)→unit_price_usd / Usd/Jpy→usd_jpy / 手数料 (Usd)→fee_usd / 税金 (Usd)→tax_usd
- cast: 提案なし (型に問題なし、business knowledge 要のため)
- hide: 提案なし (下流参照状況が flow-summary.md から要確認)
```

### 3. input-dispatch.md の書き出し

トップに **PENDING USER CONFIRMATION** のヘッダ、続いて Input dispatch 表 + 各 augment 行への Transforms 提案ブロック + (該当あれば) block 理由ブロック。最後に「`OK` で全提案受諾、行番号 + 指示で個別変更」のユーザー指示プロンプト。

書式: [input-dispatch-format.md](input-dispatch-format.md) (separately maintained)

## 出力ファイルの2状態

| 状態 | ファイル名 | マーカー |
|---|---|---|
| Proposal (PENDING) | `<session>/reports/input-dispatch.md` | frontmatter `status: pending` |
| Confirmed (final) | 同上 (上書き) | frontmatter `status: confirmed` + 各行に user decision 反映 |

main agent はユーザー応答を受けたら同じファイルを `status: confirmed` で書き直す (Phase C を再実行する必要はない)。confirmed 後は architect / builder が consume する。

## 失敗時の戻り先

| 状況 | 対処 |
|---|---|
| dispatch_inputs.py で flow.json parse 失敗 | Phase A をやり直し |
| 全 PDS Input が unresolved + Phase B 再 scan を案内したが拒否された | passthrough を選べない → augment 一択 (= 既存 PDS をそのまま使えないので新規 stg PDS を作る) で進行 |
| block (direct_db) が 1 つでも検出された | session 全体停止、ユーザーに Cloud 側整備を依頼。再開は Phase A から |
| LLM の semantic translation が業務文脈と乖離 | proposal markdown に明記、ユーザーが個別に修正指示 |

## 後段への引き渡し

- **prep-architect (analyze / decompose)**: `input-dispatch.md` を読み、stg entry の生成方針を決める。passthrough Input については stg entry を生成しない (intermediate Inputs に元 PDS 名 + LUID を直書き)。augment Input については Materialization=live_pds の stg entry を生成し Transforms 表を埋める
- **prep-builder**: `input-dispatch.md` を直接参照する必要はない (architect が decomposition-plan に embed する)
