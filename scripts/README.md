# scripts/

Skill 横断で使う共通モジュール。各 Skill 配下の scripts はここから import する形で重複を避ける。

## ファイル一覧

| ファイル | 役割 | 使用元 |
|---|---|---|
| [tableau_auth.py](tableau_auth.py) | `.env` の SERVER/SITE_NAME を使い OAuth 2.0 (Authorization Code + PKCE) でサインインし TSC `Server` インスタンスを返す共通ヘルパ (`signed_in_server()` context manager) | prep-extractor (DL), prep-deployer (publish/run) |
| [flow_io.py](flow_io.py) | `.tfl/.tflx` の zip 展開・JSON 読み込み・新 .tfl の zip 化 | prep-extractor (read), prep-builder (write) |
| [sync_agents_skills.py](sync_agents_skills.py) | `.claude/skills/*/SKILL.md` の frontmatter から Codex 向け thin wrapper (`.agents/skills/`) を冪等生成。`--check` で drift 検知 (リポ保守用、Skill からは呼ばれない) | AGENTS.md / CLAUDE.md のメンテ手順 |

## import の仕方

各 Skill のスクリプトは Repo 直下 `scripts/` を `sys.path` に追加してから import する:

```python
import sys
from pathlib import Path

# .claude/skills/<skill>/scripts/foo.py から見た Repo 直下 scripts/
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from tableau_auth import signed_in_server  # noqa: E402
from flow_io import load_flow_json         # noqa: E402
```

## 編集ガイドライン

- 副作用を持たない純関数 + 簡潔なエントリポイント
- 認証情報・パス・タイムアウト等は環境変数か明示引数で受け取る（ハードコード禁止）
- Skill 固有のビジネスロジックはここに置かず、Skill 配下の scripts に置く
