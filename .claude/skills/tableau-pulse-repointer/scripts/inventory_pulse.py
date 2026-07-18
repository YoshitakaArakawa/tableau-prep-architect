#!/usr/bin/env python3
"""Read-only inventory of Pulse metric definitions and their datasources.

design-mode Step 1. Answers the LEFT side of the pulse-repoint join:
"which Pulse definitions reference which PDS, and who follows their metrics?"

Walks ALL definitions (page_size + next_page_token — the server default of 10
silently truncates), resolves each referenced datasource via REST
datasources.get, and joins site-wide subscriptions onto each definition's
metrics. No usage filtering: every definition referencing a source-project PDS
is listed; the human decides what to keep.

Usage:
    python inventory_pulse.py --source-project <project-name> \
        --out <output_dir>/pulse-repoint-inventory.json

Cloud access is read-only. Final line: RESULT_JSON: {...}
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Repo root is 4 parents up from .claude/skills/tableau-pulse-repointer/scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tableau_auth import signed_in_server  # noqa: E402

from pulse_api import extract_referenced_fields, list_definitions, list_subscriptions  # noqa: E402


def jst_now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds")


def resolve_datasource(server: Any, ds_id: str) -> dict:
    """REST datasources.get -> {name, project} ('(unresolved)' on failure)."""
    url = (f"{server.server_address.rstrip('/')}/api/{server.version}"
           f"/sites/{server.site_id}/datasources/{ds_id}")
    req = urllib.request.Request(
        url=url,
        headers={"Accept": "application/json", "X-Tableau-Auth": server.auth_token},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            ds = json.loads(resp.read().decode())["datasource"]
        return {"name": ds["name"], "project": ds["project"]["name"]}
    except (urllib.error.HTTPError, urllib.error.URLError, KeyError) as e:
        return {"name": f"(unresolved: {getattr(e, 'code', e)})", "project": "(unresolved)"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-project", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    t0 = time.monotonic()

    with signed_in_server() as server:
        definitions = list_definitions(server)
        subscriptions = list_subscriptions(server)

        followers_by_metric: dict[str, list[dict]] = {}
        for sub in subscriptions:
            followers_by_metric.setdefault(sub["metric_id"], []).append(sub["follower"])

        ds_cache: dict[str, dict] = {}
        rows = []
        for d in definitions:
            ds_id = d["specification"]["datasource"]["id"]
            if ds_id not in ds_cache:
                ds_cache[ds_id] = resolve_datasource(server, ds_id)
            ds = ds_cache[ds_id]
            rows.append({
                "definition_id": d["metadata"]["id"],
                "name": d["metadata"]["name"],
                "datasource_id": ds_id,
                "datasource_name": ds["name"],
                "datasource_project": ds["project"],
                "in_scope": ds["project"] == args.source_project,
                "referenced_fields": extract_referenced_fields(d),
                "metrics": [
                    {
                        "metric_id": m["id"],
                        "is_default": m.get("is_default", False),
                        "specification": m.get("specification", {}),
                        "followers": followers_by_metric.get(m["id"], []),
                    }
                    for m in d.get("metrics", [])
                ],
            })

    out = {
        "generated_at": jst_now_iso(),
        "source_project": args.source_project,
        "total_definitions": len(rows),
        "definitions": rows,
        "errors": [],
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    in_scope = [r for r in rows if r["in_scope"]]
    result = {
        "out": str(out_path),
        "total_definitions": len(rows),
        "in_scope_definitions": len(in_scope),
        "subscriptions_site_wide": len(subscriptions),
        "elapsed_s": round(time.monotonic() - t0, 1),
    }
    print(f"RESULT_JSON: {json.dumps(result, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
