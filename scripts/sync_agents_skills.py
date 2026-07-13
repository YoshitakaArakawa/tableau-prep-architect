#!/usr/bin/env python3
"""Generate Codex thin-wrapper skills under .agents/skills/ from the canonical
.claude/skills/*/SKILL.md.

The canonical skills live in .claude/skills/ (Claude Code). Codex discovers
skills under .agents/skills/. Rather than duplicating the skill bodies, this
script emits a *thin wrapper* per skill: frontmatter carrying only `name` and
`description` (both copied verbatim from the canonical SKILL.md) plus a short
body that points Codex at the canonical file and at AGENTS.md's "読み替え表".

The wrapper body differs by execution mode, which is derived MECHANICALLY from
the canonical frontmatter (never hard-coded per skill name):

  - `context: fork`  -> isolated execution. Delegate to a Codex subagent.
      - `model: haiku` fork skill -> flow-worker-lite (low reasoning effort,
        for mechanical extraction tasks)
      - any other fork skill        -> flow-worker
  - no `context: fork`             -> run in the main conversation (these skills
    carry user-approval gates / failure observation and must not be delegated).

Usage:
    python scripts/sync_agents_skills.py            # write/refresh wrappers
    python scripts/sync_agents_skills.py --check     # exit 1 if any drift

stdlib only. Frontmatter is parsed with a small line parser (no PyYAML), which
tolerates a `description` value that wraps across multiple physical lines.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Repo layout: this file is at <repo>/scripts/sync_agents_skills.py, so the repo
# root is one level up.
REPO_ROOT = Path(__file__).resolve().parents[1]
CANON_DIR = REPO_ROOT / ".claude" / "skills"
CODEX_DIR = REPO_ROOT / ".agents" / "skills"

# Top-level frontmatter keys we recognise. Any line whose token before the first
# ":" is NOT one of these is treated as a continuation of the previous value
# (this is what lets a wrapped `description` span multiple physical lines).
KNOWN_KEYS = ("name", "description", "context", "agent", "model", "allowed-tools")


def parse_frontmatter(text: str) -> dict[str, str]:
    """Return the top-level scalar frontmatter of a SKILL.md as a dict.

    Only the leading `--- ... ---` block is read. Values that wrap onto
    following physical lines (lines that do not start with a known `key:`) are
    folded onto the preceding key with a single joining space, mirroring YAML
    plain-scalar folding. Block scalars (`|`, `>`) are not used by the canon and
    are not specially handled.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md does not start with a frontmatter fence")

    fm: dict[str, str] = {}
    last_key: str | None = None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        token = line.split(":", 1)[0].strip()
        if ":" in line and token in KNOWN_KEYS:
            key, _, value = line.partition(":")
            key = key.strip()
            fm[key] = value.strip()
            last_key = key
        elif last_key is not None and line.strip():
            # Continuation of a wrapped value.
            fm[last_key] = (fm[last_key] + " " + line.strip()).strip()
    return fm


def worker_agent_for(fm: dict[str, str]) -> str:
    """Pick the Codex subagent name for a fork skill from its frontmatter.

    `model: haiku` marks a lightweight, mechanical task -> the low-effort
    flow-worker-lite. Everything else uses flow-worker.
    """
    return "flow-worker-lite" if fm.get("model") == "haiku" else "flow-worker"


def render_wrapper(name: str, description: str, is_fork: bool, worker: str) -> str:
    """Build the wrapper SKILL.md text for one skill."""
    # From .agents/skills/<name>/SKILL.md up to the canonical file:
    #   .agents/skills/<name>/ -> .agents/skills -> .agents -> <repo> -> .claude/...
    # i.e. three "../" hops.
    canon_rel = f"../../../.claude/skills/{name}/SKILL.md"

    if is_fork:
        step3 = (
            f"3. この skill は隔離実行 (fork) 前提。サブエージェント "
            f"(`.codex/agents/` の {worker}、無ければ既定のサブエージェント) に委譲して実行する。"
            "サブエージェントが使えない場合はインライン実行してよいが、"
            "出力契約 (成果物はファイルへ書く・主会話へ中間 JSON を流さない・"
            "返答は要約 + Timing ブロックのみ) は必ず維持する。"
        )
    else:
        step3 = (
            "3. この skill はユーザー確認ゲート・失敗観測を含むため主会話で実行する。"
            "サブエージェントに委譲しない。"
        )

    body = (
        f"この skill の正典は [.claude/skills/{name}/SKILL.md]({canon_rel})。"
        "次の手順で実行する:\n"
        "\n"
        "1. リポジトリルートの `AGENTS.md` の「Claude Code 記法の読み替え表」を確認する (未読の場合)。\n"
        "2. 正典 SKILL.md を読み、その手順に従う。frontmatter の Claude Code 固有フィールドは読み替え表に従って解釈する。\n"
        f"{step3}\n"
    )

    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n"
        "\n"
        f"{body}"
    )


def collect_wrappers() -> dict[Path, str]:
    """Map each target wrapper path to its desired content."""
    wrappers: dict[Path, str] = {}
    for skill_md in sorted(CANON_DIR.glob("*/SKILL.md")):
        fm = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        name = fm.get("name")
        description = fm.get("description")
        if not name or not description:
            raise ValueError(f"{skill_md} missing name/description")
        is_fork = fm.get("context") == "fork"
        worker = worker_agent_for(fm) if is_fork else ""
        content = render_wrapper(name, description, is_fork, worker)
        wrappers[CODEX_DIR / name / "SKILL.md"] = content
    return wrappers


def main(argv: list[str]) -> int:
    check_only = "--check" in argv[1:]
    unknown = [a for a in argv[1:] if a != "--check"]
    if unknown:
        print(f"unknown argument(s): {' '.join(unknown)}", file=sys.stderr)
        return 2

    wrappers = collect_wrappers()

    drift = False
    # A wrapper whose canonical skill was deleted/renamed would otherwise
    # linger and keep passing --check.
    orphans = [
        p for p in CODEX_DIR.glob("*/SKILL.md") if p not in wrappers
    ] if CODEX_DIR.exists() else []
    for path in orphans:
        rel = path.relative_to(REPO_ROOT).as_posix()
        drift = True
        if check_only:
            print(f"drift: {rel} has no canonical skill (orphan)", file=sys.stderr)
        else:
            path.unlink()
            if not any(path.parent.iterdir()):
                path.parent.rmdir()
            print(f"removed orphan {rel}")
    for path, content in wrappers.items():
        rel = path.relative_to(REPO_ROOT).as_posix()
        existing = path.read_text(encoding="utf-8") if path.exists() else None
        if existing == content:
            continue
        drift = True
        if check_only:
            reason = "missing" if existing is None else "out of date"
            print(f"drift: {rel} is {reason}", file=sys.stderr)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            print(f"wrote {rel}")

    if check_only:
        if drift:
            print("wrappers out of sync; run: python scripts/sync_agents_skills.py",
                  file=sys.stderr)
            return 1
        print(f"ok: {len(wrappers)} wrapper(s) in sync")
        return 0

    if not drift:
        print(f"ok: {len(wrappers)} wrapper(s) already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
