#!/usr/bin/env python3
"""Get the status of a Tableau job (typically a Prep flow run).

finishCode meanings:
    0 = Success
    1 = Failed
    2 = Cancelled
    (completed_at is None) = InProgress / Pending

Exit code mirrors the job: 0 on success, non-zero otherwise (useful for CI).

Usage:
    python get_job_status.py --job-id <luid>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))

from tableau_auth import sign_in_server  # noqa: E402


FINISH_CODES = {0: "Success", 1: "Failed", 2: "Cancelled"}


def parse_args():
    p = argparse.ArgumentParser(description="Get Tableau job status by job id")
    p.add_argument("--job-id", required=True)
    return p.parse_args()


def main():
    args = parse_args()
    server, auth = sign_in_server()
    with server.auth.sign_in(auth):
        job = server.jobs.get_by_id(args.job_id)
        print(f"Job id:        {job.id}")
        print(f"Job type:      {job.type}")
        print(f"Created at:    {job.created_at}")
        print(f"Started at:    {job.started_at}")
        print(f"Completed at:  {job.completed_at}")
        if job.completed_at is not None:
            code = job.finish_code
            label = FINISH_CODES.get(code, f"Unknown({code})")
            print(f"Finish:        {label} (code={code})")
            notes = getattr(job, "notes", None)
            if code != 0 and notes:
                print(f"Notes:         {notes}")
            sys.exit(0 if code == 0 else 1)
        else:
            print("Status:        InProgress / Pending")
            sys.exit(0)


if __name__ == "__main__":
    main()
