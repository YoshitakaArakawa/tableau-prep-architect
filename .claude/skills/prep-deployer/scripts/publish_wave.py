#!/usr/bin/env python3
"""Publish multiple .tfl/.tflx files to a single target project in parallel.

For one "wave" of flows that have no intra-wave dependency (e.g. all stg
flows, or `fct_*` within marts, or `rpt_*` within marts AFTER fct/dim have
been published+run+patched), this script saves the per-publish round-trip
cost by issuing the publishes concurrently from a single sign-in session.

Use it when:
  - You have N flows in the same layer whose Inputs are already satisfied
    (upstream PDSes published) and that share the same target project.
  - You want a single auth/sign-in for the whole wave (matches the design
    used in `run_layer.py` for parallel job polling).

Do NOT use it across layers or across waves with intra-list dependencies:
  - rpt_*.tfl must wait until fct_*/dim_*.tfl are published+run+patched, so
    they belong to a separate wave (separate invocation).
  - int_b depending on int_a's PDS belongs to a later wave than int_a.

Approval model is identical to `publish_flow.py`: non-interactive, session
intake (CLAUDE.md step 0) approves all writes in advance.

Usage:

    # All stg flows in one wave (no intra-layer deps)
    python publish_wave.py \\
      --project-id <stg-project-luid> \\
      --file flows/staging/stg_a.tfl \\
      --file flows/staging/stg_b.tfl

    # OR by project path
    python publish_wave.py \\
      --project-path "99_Sandbox/<target>/flows/stg" \\
      --file flows/staging/stg_a.tfl --file flows/staging/stg_b.tfl

    # Tune parallelism (default: as many threads as files, max 8)
    python publish_wave.py --project-id <luid> --max-workers 4 \\
      --file flows/marts/fct_a.tfl --file flows/marts/fct_b.tfl
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))

import tableauserverclient as TSC  # noqa: E402

from tableau_auth import signed_in_server  # noqa: E402


MAX_WORKERS_HARD_CAP = 8


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Publish multiple flows to one project in parallel "
                    "(single sign-in session)"
    )
    p.add_argument("--file", action="append", required=True,
                   help="Path to .tfl/.tflx (repeat per file in the wave)")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--project-id", help="Target project LUID")
    grp.add_argument("--project-path",
                     help="Project path like 'Parent/Child' (slash-separated)")
    p.add_argument("--mode", choices=["CreateNew", "Overwrite"], default="CreateNew")
    p.add_argument("--max-workers", type=int, default=None,
                   help=f"Max parallel publish threads "
                        f"(default: min(len(files), {MAX_WORKERS_HARD_CAP}))")
    return p.parse_args()


def resolve_project_id(server, *, project_id, project_path):
    if project_id:
        return project_id
    parts = [seg.strip() for seg in project_path.split("/") if seg.strip()]
    if not parts:
        sys.exit("ERROR: --project-path must not be empty")
    all_projects, _ = server.projects.get(req_options=TSC.RequestOptions(pagesize=1000))
    parent_id = None
    for name in parts:
        match = [p for p in all_projects if p.name == name and p.parent_id == parent_id]
        if not match:
            sys.exit(f"ERROR: Project segment '{name}' not found "
                     f"(parent_id={parent_id})")
        if len(match) > 1:
            sys.exit(f"ERROR: Ambiguous project name '{name}' at this level")
        parent_id = match[0].id
    return parent_id


def publish_one(server, *, file_path: Path, project_id: str,
                mode: str) -> tuple[str, str, str | None]:
    """Publish one .tfl. Returns (name, luid, error_msg_or_none)."""
    try:
        flow_item = TSC.FlowItem(project_id=project_id, name=file_path.stem)
        published = server.flows.publish(
            flow_item,
            str(file_path),
            mode=getattr(TSC.Server.PublishMode, mode),
        )
        return (published.name, published.id, None)
    except Exception as exc:  # broad: TSC raises various subclasses
        return (file_path.stem, "", f"{type(exc).__name__}: {exc}")


def main() -> int:
    args = parse_args()
    files = [Path(f).resolve() for f in args.file]
    for fp in files:
        if not fp.exists():
            sys.exit(f"ERROR: File not found: {fp}")

    workers = args.max_workers or min(len(files), MAX_WORKERS_HARD_CAP)
    workers = max(1, min(workers, MAX_WORKERS_HARD_CAP))

    failures: list[tuple[str, str]] = []
    with signed_in_server() as server:
        project_id = resolve_project_id(
            server, project_id=args.project_id, project_path=args.project_path
        )
        print(f"[publish_wave] target project_id={project_id}; "
              f"{len(files)} file(s); workers={workers}")

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {
                ex.submit(publish_one, server,
                          file_path=fp, project_id=project_id, mode=args.mode): fp
                for fp in files
            }
            for fut in as_completed(futures):
                name, luid, err = fut.result()
                if err is None:
                    print(f"  [ok]   {name} -> id={luid}")
                else:
                    print(f"  [fail] {name}: {err}")
                    failures.append((name, err))

    if failures:
        print(f"\n[publish_wave] {len(failures)} failure(s):", file=sys.stderr)
        for name, err in failures:
            print(f"  - {name}: {err}", file=sys.stderr)
        return 1
    print(f"\n[publish_wave] all {len(files)} flow(s) published OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
