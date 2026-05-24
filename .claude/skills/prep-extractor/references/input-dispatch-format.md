---
purpose: prep-extractor Phase C が出力する input-dispatch.md の書式仕様
fetched_at: 2026-05-24
note: status=pending (proposal) と status=confirmed (user 合意済) の 2 状態。後段 architect / decomposer が consume する frontmatter は固定キー
---

# input-dispatch-format

`work/<session>/reports/input-dispatch.md` の書式。Phase C で生成、ユーザー確認を経て status を `confirmed` に書き換え、architect / decomposer が読む。

## 状態

| status | 意味 | 書き換える主体 |
|---|---|---|
| `pending` | Phase C の LLM が提案を書いた状態。ユーザー確認待ち | Phase C スクリプト + fork 内 LLM |
| `confirmed` | ユーザー応答を受けて main agent が反映済み。後段 consumer が読む対象 | main agent (Phase C 再実行は不要) |

## 構造

```markdown
---
status: pending          # or "confirmed"
source_flow: work/<session>/flow.json
deploy_context: work/<session>/reports/deploy-context.md
generated_at: <ISO-8601 JST>
confirmed_at: <ISO-8601 JST or null>
input_count: <N>
kind_counts:
  pds: <n>
  vconn: <n>
  direct_db: <n>
  extract: <n>
  unknown: <n>
blocks_present: <true | false>  # true なら session 停止
---

# Input dispatch: <flow-name>

## Summary

(1-2 行で全体状況。例: "vconn 1 / pds 1。passthrough + augment で進行可能。block なし。")

## Inputs

| # | Input ノード名 | kind | 推奨方針 | 根拠 | PDS LUID / vconn LUID |
|---|---|---|---|---|---|
| 1 | <name> | pds | **passthrough** | (理由 1 文) | `<luid>` (deploy-context.md 解決済) |
| 2 | <name> | vconn | **augment** | (理由 1 文) | `<vconn-luid>` (flow.json から直接抽出) |
| 3 | <name> | direct_db | **block** | Prep に DB 認証情報を入れない方針 | n/a |

## Per-input proposals

### #1 <name> (passthrough)

passthrough なので新規 stg PDS は作らない。intermediate flow は以下 PDS を Input として直接参照する:

- Project path: `<path>`
- PDS name: `<name>`
- PDS LUID: `<luid>`

ユーザー確認事項: この PDS で本当に整形済みか? (`stg_*` 用の rename / cast / hide が不要か)

### #2 <name> (augment)

vconn から `stg_<name>` を新規 publish。

**policy 級 Transforms 提案** (詳細表は decompose で確認):

- 全 N 列の caption を snake_case 化:
  - ASCII 列 (M 個): 機械変換 (例: `Update Date` → `update_date`)
  - 非 ASCII 列 (M' 個): semantic translation (例: `数量` → `quantity`, `単価 (Usd)` → `unit_price_usd`)
- cast: なし (型 OK / 業務文脈要のため AI 側からの強い提案は控える)
- hide: なし (下流参照状況要確認)

### #3 <name> (block)

(direct_db / extract / unknown のときの escalation 文をそのまま書く。`phase-c-procedure.md §LLM の責務 2` 参照)

## User confirmation

OK で全提案を受諾するか、行番号 + 指示で個別変更:

- `OK`
- `#1 を augment に変更 (このPDSが実は raw extract で整形必要)`
- `#2 の rename を 全部 user_<番号> 形式に変更 (semantic translation を保留)`
- 等

応答後、main agent が本ファイルの status を `confirmed` に変えて user decision を反映:

\`\`\`yaml
---
status: confirmed
...
confirmed_at: 2026-05-24T11:30:00+09:00
---
\`\`\`

各 Input セクションの「推奨方針」とその下の Transforms 提案を user 合意版に書き換える。元の proposal は履歴として残したい場合は `## Proposal history` セクションを末尾に追加 (オプション)。
```

## 後段の consume 規約

| consumer | 読む内容 | 用途 |
|---|---|---|
| prep-architect (analyze) | `## Inputs` 表 + frontmatter `blocks_present` | block ありなら decompose しない (session 停止) |
| prep-architect (decompose) | 各 Input の "推奨方針" + augment 行の Transforms 提案 | stg entry の Materialization 決定、Transforms (column-level) 表の初期値 |
| prep-builder | (architect の decomposition-plan 経由で間接的に) | input-dispatch.md は直接参照しない |
| prep-deployer | 同上 | input-dispatch.md は直接参照しない |

architect は decompose 時に Transforms 提案を **decomposition-plan.md の `Transforms (column-level)` 表に転記** する (詳細値の調整は plan レビュー時に再度ユーザーから受ける、二段確認)。
