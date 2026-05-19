# Skill Timing Contract

`context: fork` で動く Skill は内部時間が主会話から見えないため、各 Skill の **主会話への戻り値メッセージの末尾** に `## Timing` ブロックを必ず含める。fork 内部のどの phase で時間が溶けたかを観測できるようにするため。

ファイル出力 (例: `analysis-*.md`, `flow-summary.md`) には Timing を入れない (後段 Skill が消費するので、ノイズになる)。あくまで **主会話への戻り値** にのみ書く。

## フォーマット

```
## Timing

- start:   YYYYMMDD HH:MM:SS JST
- end:     YYYYMMDD HH:MM:SS JST
- elapsed: NNNs (Xm Ys)
- breakdown:
  - <phase 名>: NNNs
  - <phase 名>: NNNs
```

- 時刻は **JST 固定**、`YYYYMMDD HH:MM:SS JST` 表記 (個人ルール: 日付は YYYYMMDD)
- `elapsed` は秒単位 + 人間可読の `(Xm Ys)`
- `breakdown` は各 Skill が自分の主要 phase を 3-6 項目列挙する (細かすぎる粒度は不要)
- 秒未満は四捨五入

## 各 Skill の breakdown 推奨項目

| Skill | breakdown |
|---|---|
| prep-extractor Phase A | input load (.tfl 展開) / topology 抽出 / actions inventory / Mermaid 生成 / write |
| prep-extractor Phase B | project tree fetch / parent walk + naming scan / write |
| prep-architect | input read (flow-summary etc.) / analyze 本体 / decompose 本体 / self-check / write |
| prep-builder | plan parse / source flow load / per-tfl build (8 .tfl の合計) / manifest init / write |
| prep-output-comparator | pair resolve / metadata API (N 件) / query-datasource (N 件) / flag check / write |

## 実装ガイド

Python script の中で計時する場合:

```python
import time
t0 = time.monotonic()
phase_starts = {"input_load": t0}
# ... do work ...
phase_starts["topology"] = time.monotonic()
# ... do work ...
phase_starts["write"] = time.monotonic()
# ... do work ...
end = time.monotonic()
# elapsed = end - t0; breakdown = diffs between consecutive entries
```

Skill が複数のスクリプトを呼ぶ場合 (例: prep-extractor は Python + bash の混在) は、各スクリプトの stderr / stdout の最終行で `[timing] phase=<name> elapsed=<sec>` を吐き、Skill 本体がそれを集約する。

## 例

```
## Timing

- start:   20260519 20:27:52 JST
- end:     20260519 20:38:31 JST
- elapsed: 639s (10m 39s)
- breakdown:
  - input read: 8s
  - analyze: 220s
  - decompose: 360s
  - self-check: 35s
  - write: 16s
```

主会話側は subagent fork のオーバーヘッド (sign-in / token 等) を含めた wall clock しか観測できないため、Skill 内 breakdown が **fork 内部の真の時間配分** を明らかにする唯一の手段になる。
