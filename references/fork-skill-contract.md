---
purpose: context:fork で動く後工程 Skill (tableau-pds-comparator / tableau-prep-schedule-designer / tableau-workbook-repointer) が共通で守る呼び出し契約を 1 箇所に集約する。caller 入力の渡し方・戻り値の Timing・失敗時の停止規律・認証失効の扱いの 4 点。多くは read-only だが、tableau-workbook-repointer の repoint モード (WB republish) のようにサーバー書込を持つモードにも同じ契約が適用される
note: 全 fork Skill に共通する骨だけを規定する。各 Skill 固有の入力表・固有の「よくある失敗」パターン列挙は各 SKILL.md に残す。書き込み系 (tableau-prep-deployer / tableau-pds-augmenter / tableau-pds-backfiller) は承認・失敗観測を主会話で扱うため fork せず、この契約 (特に §2 Timing) は適用されない — 適用是非は各 SKILL.md が明示する
---

# Fork Skill Contract

`context: fork` で動く Skill は forked subagent コンテキストで実行され、**主会話の履歴を見られない**。この非対称から来る共通の呼び出し契約を規定する。この 4 点は全 fork Skill に共通するので、各 SKILL.md では本ファイルへの参照 1 行で足り、Skill 固有の入力・失敗パターンだけを各 SKILL.md に書く。

## 1. caller 入力は文章で明示する

fork は会話履歴を渡せないため、caller (メインエージェント) は起動時に **必要な入力をすべて文章で明示** する。各 Skill が必須とする入力の一覧 (パス・モード・ポリシー等) は各 SKILL.md の「Caller から渡される入力」表が正。fork 側は「会話に出ていたはず」を前提にせず、明示されなかった必須入力は caller に差し戻す。

## 2. 戻り値末尾に `## Timing` ブロック

fork 内部の経過時間は主会話から見えないため、**主会話への戻り値メッセージの末尾** に `## Timing` ブロックを必ず含める。フォーマットと Skill 別 breakdown の推奨項目は [skill-timing-contract.md](skill-timing-contract.md)。verify モードを持つ Skill は overall_verdict と要対応事項の要約も戻り値に含める (Timing とは別)。ファイル出力には Timing を入れない。

## 3. 失敗時は停止して caller に返す

スクリプトや MCP 呼び出しが失敗したら **その時点で停止し、caller にエラーを返す**。後工程 Skill は autonomous-recovery をしない — リトライするか・入力を直すか・人間作業を挟むかの判断は caller (メインエージェント) の責務。**サーバー書込を持つモード (repoint の WB republish) も同じ**で、失敗した publish を fork 内で自律リトライしない (本番資産への再書込判断はユーザー承認系の caller 側に置く)。各 Skill 固有の「よくある失敗」パターンとその差し戻し文言は各 SKILL.md に列挙する。

## 4. 認証失効は caller に返す

サーバーに触る Skill (TSC 経由なら `scripts/tableau_auth.py` の `signed_in_server()`) は、**認証失効時に fork 内で放置してタイムアウトさせず、caller にその旨を返す**。ブラウザ再サインインはユーザー在席が要るため主会話側で捌く。`python scripts/tableau_auth.py status` の確認と再サインインを caller に依頼する。MCP 経由の 401 の扱いは各 Skill の recipe を参照。
