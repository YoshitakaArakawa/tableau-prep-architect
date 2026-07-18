# scripts/

Skill 横断で使う共通モジュールと orchestrator。各 Skill 配下の scripts はここから import または CLI として呼び出す形で重複を避ける。

## ファイル一覧

| ファイル | 役割 | 使用元 |
|---|---|---|
| [tableau_auth.py](tableau_auth.py) | `.env` の SERVER/SITE_NAME を使い OAuth 2.0 (Authorization Code + PKCE) でサインインし TSC `Server` インスタンスを返す共通ヘルパ (`signed_in_server()` context manager) | prep-extractor (DL), prep-deployer (publish/run) |
| [flow_io.py](flow_io.py) | `.tfl/.tflx` の zip 展開・JSON 読み込み・新 .tfl の zip 化。ノード操作の低レベル primitives (`copy_source_node`, `add_pds_input` 等) と verifiers もここ | prep-extractor (read), prep-builder (write) |
| [plan_model.py](plan_model.py) | decomposition plan.json の共有モデル。load + 構造検証 (`load_plan`)、step 番号↔ノード UUID 解決 (`StepResolver`)、kind=tfl の wiring グラフ計算 (`compute_flow_graph`)、deploy-context.md パース。REST 呼び出しなし (ローカル計算のみ) | prep-architect (gen_plan_skeleton / render_plan_md / plan_html), prep-builder (build_from_plan) |
| [build_helpers.py](build_helpers.py) | build スクリプトが毎回再実装していた組み立てヘルパ (`empty_flow`, `add_edge`, `split_supertransform_actions`, `transplant_source_input` 等) の共通化。flow_io の primitives とセッション別 build スクリプトの中間層 | prep-builder (build_from_plan, 中断時の LLM 個別対処), セッション別 build スクリプト |
| [publish_manifest.py](publish_manifest.py) | セッションの `reports/publish-manifest.json` の read/write/update CLI (`init` / `update-publish` / `update-run` / `resolve-luids`)。スキーマは references/publish-manifest-format.md | prep-builder (init), prep-deployer (update-publish / update-run / resolve-luids), prep-output-comparator (読み取り), run_layer.py |
| [run_layer.py](run_layer.py) | manifest の 1 レイヤ内の未 run flow を `--no-wait` で一括発火し、単一サインインセッションで全 jobId を polling する run orchestrator (server-side 並列)。完了ごとに publish_manifest.py update-run を呼ぶ | prep-deployer (レイヤ単位 run) |
| [sync_agents_skills.py](sync_agents_skills.py) | `.claude/skills/*/SKILL.md` の frontmatter から Codex 向け thin wrapper (`.agents/skills/`) を冪等生成。`--check` で drift 検知 (リポ保守用、Skill からは呼ばれない) | AGENTS.md / CLAUDE.md のメンテ手順 |
| [pulse_api.py](pulse_api.py) | Tableau Pulse REST (`/api/-/pulse/...`) の共通クライアント。ページ追従つき definitions / subscriptions 列挙、定義 payload 組み立て、参照フィールド抽出、insight probe | prep-pulse-repointer (全モード), consumer_probe.py |
| [consumer_probe.py](consumer_probe.py) | 旧 output PDS ごとの consumer 数 (downstream WB / Pulse 定義 / follower 付き定義) を read-only で実測し、repoint 工程の要否推奨 (`repoint` / `cleanup_only` / `none`) を返す step 0b 用 CLI | prep-migrate step 0b (main agent が実行、Stop 1 の提示材料) |

## import の仕方

各 Skill のスクリプトは Repo 直下 `scripts/` を `sys.path` に追加してから import する:

```python
import sys
from pathlib import Path

# .claude/skills/<skill>/scripts/foo.py から見た Repo 直下 scripts/
# (work/<session>/scripts/ から使う場合は parents[3])
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))

from tableau_auth import signed_in_server  # noqa: E402
from flow_io import load_flow_json         # noqa: E402
```

## 編集ガイドライン

- 基本は副作用を持たない純関数 + 簡潔なエントリポイント。orchestrator (run_layer.py) と manifest 更新 CLI (publish_manifest.py) は例外だが、副作用の範囲を docstring に明記する
- 認証情報・パス・タイムアウト等は環境変数か明示引数で受け取る（ハードコード禁止）
- Skill 固有のビジネスロジックはここに置かず、Skill 配下の scripts に置く
