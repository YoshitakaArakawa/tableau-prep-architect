#!/usr/bin/env python3
"""Run all pending flows in one manifest layer with server-side parallelism.

For each decomposed flow in the target layer that is `publish.status=published`
and `run.status!=success`, this script:

  1. Fires `run_flow.py --no-wait` sequentially (sign_in / sign_out closes in
     ~100ms per call, so signins never overlap and the PAT's single active
     session is never contested). Server-side runs the jobs in parallel.
  2. Holds ONE sign-in session in this process and polls every collected
     jobId with `server.jobs.get_by_id`. Polling reads only; no token churn.
  3. Calls `publish_manifest.py update-run` per completed flow.

Why not just run --wait calls in parallel: Tableau Cloud enforces 1 active
session per PAT, so a second sign_in revokes the prior token server-side and
the first poller dies with 401. See run-and-poll.md (`§並列実行と排他`) for
the verified root cause.

Exit code: 0 if every flow finished with finish_code=0, else 1.
Recovery is left to the caller (this script does not retry).

Usage:
    python scripts/run_layer.py \
      --manifest work/<session>/reports/publish-manifest.json \
      --layer staging \
      --poll-interval 15 \
      --timeout 1800
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_FLOW_PY = REPO_ROOT / ".claude" / "skills" / "prep-deployer" / "scripts" / "run_flow.py"
PUBLISH_MANIFEST_PY = REPO_ROOT / "scripts" / "publish_manifest.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))

from tableau_auth import signed_in_server  # noqa: E402


FINISH_CODES = {0: "Success", 1: "Failed", 2: "Cancelled"}
LAYER_CHOICES = ("staging", "intermediate", "marts")
RESULT_JSON_PREFIX = "RESULT_JSON: "


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Server-side parallel run of all pending flows in one manifest layer"
    )
    p.add_argument("--manifest", required=True,
                   help="Path to publish-manifest.json")
    p.add_argument("--layer", required=True, choices=LAYER_CHOICES,
                   help="Layer to run (staging / intermediate / marts)")
    p.add_argument("--poll-interval", type=int, default=15,
                   help="Polling interval in seconds (default: 15)")
    p.add_argument("--timeout", type=int, default=1800,
                   help="Total polling timeout in seconds (default: 1800)")
    return p.parse_args()


def select_pending(manifest: dict, layer: str) -> list[dict]:
    """Pick decomposed_flows that are publish=published and run!=success."""
    out: list[dict] = []
    for df in manifest.get("decomposed_flows", []):
        if df.get("layer") != layer:
            continue
        if (df.get("publish") or {}).get("status") != "published":
            continue
        if not (df.get("publish") or {}).get("flow_luid"):
            continue
        if (df.get("run") or {}).get("status") == "success":
            continue
        out.append(df)
    return out


def parse_result_json(stdout: str) -> dict | None:
    for line in stdout.splitlines():
        if line.startswith(RESULT_JSON_PREFIX):
            return json.loads(line[len(RESULT_JSON_PREFIX):])
    return None


def fire_run(flow_luid: str) -> dict:
    """Invoke `run_flow.py --no-wait` and return the parsed RESULT_JSON payload."""
    cmd = [sys.executable, str(RUN_FLOW_PY), "--flow-id", flow_luid, "--no-wait"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    payload = parse_result_json(proc.stdout)
    if proc.returncode != 0 or payload is None:
        raise RuntimeError(
            f"run_flow.py failed for flow_luid={flow_luid} "
            f"(returncode={proc.returncode}).\nstdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )
    return payload


def update_manifest_run(manifest_path: str, flow_name: str, finish_code: int) -> None:
    cmd = [
        sys.executable, str(PUBLISH_MANIFEST_PY), "update-run",
        "--manifest", manifest_path,
        "--flow-name", flow_name,
        "--finish-code", str(finish_code),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"publish_manifest.py update-run failed for flow_name={flow_name}.\n"
            f"stderr:\n{proc.stderr}"
        )


def main() -> int:
    args = parse_args()
    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pending = select_pending(manifest, args.layer)

    if not pending:
        print(f"[run_layer] No pending flows in layer={args.layer}. Nothing to do.")
        return 0

    print(f"[run_layer] Found {len(pending)} pending flow(s) in layer={args.layer}:")
    for df in pending:
        print(f"  - {df['name']} (flow_luid={df['publish']['flow_luid']})")

    # Step 1: fire each flow sequentially with --no-wait. Each subprocess does
    # its own sign_in / POST /flows/{id}/run / sign_out within ~100ms, so the
    # PAT's single active session is never contested by these starts.
    jobs: list[dict] = []
    print(f"\n[run_layer] Firing {len(pending)} flow(s) with --no-wait...")
    for df in pending:
        payload = fire_run(df["publish"]["flow_luid"])
        jobs.append({"flow_name": df["name"], "job_id": payload["jobId"]})
        print(f"  [ok] {df['name']} -> jobId={payload['jobId']}")

    # Step 2: hold ONE sign-in session and poll every jobId. Reads are cheap
    # and never trigger token churn.
    print(
        f"\n[run_layer] Polling {len(jobs)} job(s) from a single session "
        f"(interval={args.poll_interval}s, timeout={args.timeout}s)..."
    )
    results: dict[str, dict] = {}
    start = time.time()
    with signed_in_server() as server:
        remaining = list(jobs)
        while remaining and (time.time() - start) < args.timeout:
            still: list[dict] = []
            for j in remaining:
                job = server.jobs.get_by_id(j["job_id"])
                if job.completed_at is None:
                    still.append(j)
                    continue
                code = job.finish_code
                label = FINISH_CODES.get(code, f"Unknown({code})")
                elapsed = int(time.time() - start)
                results[j["flow_name"]] = {
                    "finish_code": code,
                    "label": label,
                    "duration_sec": elapsed,
                    "notes": getattr(job, "notes", None),
                }
                print(f"  [{elapsed:>5}s] {j['flow_name']} -> {label} (finish_code={code})")
            if still and (time.time() - start) < args.timeout:
                time.sleep(args.poll_interval)
            remaining = still

        for j in remaining:
            results[j["flow_name"]] = {
                "finish_code": None,
                "label": "Timeout",
                "duration_sec": args.timeout,
                "notes": f"Timed out after {args.timeout}s",
            }
            print(f"  [TIMEOUT] {j['flow_name']} did not complete within {args.timeout}s")

    # Step 3: persist finish_code for every completed run.
    print("\n[run_layer] Updating manifest...")
    for flow_name, res in results.items():
        if res["finish_code"] is None:
            continue
        update_manifest_run(str(manifest_path), flow_name, res["finish_code"])
        print(f"  [ok] {flow_name}: finish_code={res['finish_code']}")

    print(f"\n[run_layer] Layer={args.layer} summary:")
    failed: list[str] = []
    for flow_name, res in results.items():
        print(f"  {flow_name}: {res['label']} (duration={res['duration_sec']}s)")
        if res["finish_code"] != 0:
            failed.append(flow_name)

    if failed:
        print(
            f"\n[run_layer] FAILED: {len(failed)} flow(s): {failed}",
            file=sys.stderr,
        )
        return 1
    print(f"\n[run_layer] All {len(results)} flow(s) succeeded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
