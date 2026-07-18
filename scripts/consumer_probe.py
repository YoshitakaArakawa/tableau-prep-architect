#!/usr/bin/env python3
"""Read-only consumer probe: who consumes these (old) published data sources?

Answers, per PDS, the step-0b discovery question "is a repoint phase needed at
all?" BEFORE the user has to decide crosscut phases (Q2b): counts downstream
workbooks (Metadata API lineage) and Tableau Pulse metric definitions
(definitions walk; WB lineage does NOT show Pulse consumption). Detailed
per-asset inventories stay in the repointers' design modes — this probe only
measures enough to ground the Stop 1 recommendation.

Usage:
    python scripts/consumer_probe.py \
        --pds-name "<old output PDS name>" [--pds-name <...> ...] \
        [--pds <luid> ...] [--out <path>/consumer-probe.json]

Names are resolved via REST (exact match); ambiguous names (same name in
multiple projects) yield one row per match. Cloud access is read-only.
Final line: RESULT_JSON: {...}
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pulse_api import list_definitions, list_subscriptions  # noqa: E402
from tableau_auth import signed_in_server  # noqa: E402

# Lineage direction confirmed reliable in this repo (see prep-workbook-repointer
# references/lineage-model.md); the reverse direction is deliberately not used.
LINEAGE_QUERY = """
{
  publishedDatasources {
    luid
    downstreamWorkbooks { luid }
  }
}
"""


def resolve_names(server, names: list[str]) -> tuple[list[dict], list[str]]:
    """Exact-name REST lookup -> [{luid, name, project}], plus not-found names."""
    rows, missing = [], []
    base = server.server_address.rstrip("/")
    for name in names:
        url = (f"{base}/api/{server.version}/sites/{server.site_id}"
               f"/datasources?filter=name:eq:{urllib.parse.quote(name)}")
        req = urllib.request.Request(
            url=url, headers={"Accept": "application/json",
                              "X-Tableau-Auth": server.auth_token})
        with urllib.request.urlopen(req, timeout=30) as resp:
            matches = json.loads(resp.read().decode())["datasources"].get("datasource", [])
        if not matches:
            missing.append(name)
        for ds in matches:
            rows.append({"luid": ds["id"], "name": ds["name"],
                         "project": ds["project"]["name"]})
    return rows, missing


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pds-name", action="append", default=[])
    parser.add_argument("--pds", action="append", default=[],
                        help="PDS luid (alternative to --pds-name)")
    parser.add_argument("--out", help="optional JSON output path")
    args = parser.parse_args()
    if not args.pds_name and not args.pds:
        sys.exit("ERROR: pass at least one --pds-name or --pds")
    t0 = time.monotonic()

    with signed_in_server() as server:
        targets, missing = resolve_names(server, args.pds_name)
        known = {t["luid"] for t in targets}
        targets += [{"luid": l, "name": "(luid 指定)", "project": "?"}
                    for l in args.pds if l not in known]

        # one lineage query + one Pulse walk cover every target
        lineage = server.metadata.query(query=LINEAGE_QUERY)
        errors = [e.get("message", "?") for e in (lineage.get("errors") or [])]
        wb_by_pds = {
            p["luid"]: len(p.get("downstreamWorkbooks") or [])
            for p in (lineage.get("data") or {}).get("publishedDatasources") or []
        }
        definitions = list_definitions(server)
        followed_metric_ids = {s["metric_id"] for s in list_subscriptions(server)}

        results = []
        for t in targets:
            defs = [d for d in definitions
                    if d["specification"]["datasource"]["id"] == t["luid"]]
            followed = [d for d in defs
                        if any(m["id"] in followed_metric_ids
                               for m in d.get("metrics", []))]
            wb = wb_by_pds.get(t["luid"], 0)
            # three-way recommendation: active consumers -> repoint phase needed;
            # only unfollowed Pulse definitions -> no repoint, but they must be
            # discarded before the old PDS retires (else they break);
            # nothing at all -> no phase needed.
            if wb or followed:
                recommendation = "repoint"
            elif defs:
                recommendation = "cleanup_only"
            else:
                recommendation = "none"
            results.append({
                **t,
                "workbooks": wb,
                "pulse_definitions": len(defs),
                "pulse_definitions_followed": len(followed),
                "recommendation": recommendation,
            })

    REC_LABEL = {
        "repoint": "→ repoint 推奨 (稼働中の consumer あり)",
        "cleanup_only": "→ repoint 不要 / 未 follow の Pulse 定義の破棄整理のみ",
        "none": "→ consumer なし",
    }
    for r in results:
        print(f"{r['name']!r} ({r['project']}): WB {r['workbooks']} 件 / "
              f"Pulse 定義 {r['pulse_definitions']} 件"
              f" (follower 付き {r['pulse_definitions_followed']}) "
              f"{REC_LABEL[r['recommendation']]}")

    payload = {"results": results, "not_found": missing, "metadata_errors": errors}
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                            encoding="utf-8")

    result = {
        "probed": len(results),
        "repoint_recommended": sum(1 for r in results if r["recommendation"] == "repoint"),
        "cleanup_only": sum(1 for r in results if r["recommendation"] == "cleanup_only"),
        "not_found": missing,
        "metadata_errors": errors,
        "out": args.out,
        "elapsed_s": round(time.monotonic() - t0, 1),
    }
    print(f"RESULT_JSON: {json.dumps(result, ensure_ascii=False)}")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
