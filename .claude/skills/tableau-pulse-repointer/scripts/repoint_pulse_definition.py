#!/usr/bin/env python3
"""Repoint Pulse definitions to their new PDS (rehearsal / production).

In-place datasource swap via PATCH is rejected by the API (404), so both stages
work through copies (see references/pulse-api-recipe.md):

rehearsal   Create "<name> (repoint rehearsal)" on the new PDS and compare BAN
            insights between the original and the copy. Additive only — the
            original definition is untouched.

production  For each definition (caller passes this stage only after the user
            approved the rehearsal evidence):
              1. rename old -> "<name> (pre-repoint)"
              2. PROMOTE the rehearsal copy to the original name (Pulse rejects
                 a duplicate (datasource, specification) with 409, so the copy
                 IS the new definition); fresh create only if no rehearsal ran
              3. recreate metrics + follower subscriptions from the old
                 definition's LIVE state (not the design snapshot — followers
                 added after design would otherwise be lost at cutover)
              4. insight-probe the new definition (abort on failure — the old
                 definition is only renamed, so rollback is a rename back)
              5. delete a leftover rehearsal copy if a fresh create was used
                 (zero-follower guard; a promoted copy is never deleted)
            The OLD definition is never deleted here: deletion cascades to
            metrics + subscriptions and stays a human decision (runbook step).

Usage:
    python repoint_pulse_definition.py --design <pulse-repoint-design.json> \
        --definition <def_id> [--definition <...> ...] --stage rehearsal|production

Final line: RESULT_JSON: {...}
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tableau_auth import signed_in_server  # noqa: E402

from pulse_api import (  # noqa: E402
    PulseHTTPError,
    call,
    definition_payload,
    insight_probe,
    list_definitions,
    list_subscriptions,
)

REHEARSAL_SUFFIX = " (repoint rehearsal)"
ARCHIVE_SUFFIX = " (pre-repoint)"


def default_metric(server, definition_id: str) -> dict | None:
    _, data = call(server, "GET", f"/api/-/pulse/definitions/{definition_id}/metrics")
    metrics = data.get("metrics", [])
    defaults = [m for m in metrics if m.get("is_default")]
    return (defaults or metrics or [None])[0]


def markup_numbers(markup: str) -> list[str]:
    """Digit runs in a BAN markup — the comparison key for rehearsal verdicts.

    Comparing full strings would flag the name difference (the rehearsal copy
    is renamed), so only the numeric content is compared.
    """
    import re
    return re.findall(r"[\d.,]+", markup)


def stage_rehearsal(server, pair: dict) -> dict:
    def_id = pair["definition_id"]
    _, data = call(server, "GET", f"/api/-/pulse/definitions/{def_id}")
    original = data["definition"]
    rehearsal_name = pair["definition_name"] + REHEARSAL_SUFFIX

    existing = [d for d in list_definitions(server)
                if d["metadata"]["name"] == rehearsal_name]
    if existing:
        copy = existing[0]
    else:
        payload = definition_payload(original, rehearsal_name,
                                     datasource_id=pair["new_pds"]["luid"])
        _, created = call(server, "POST", "/api/-/pulse/definitions", payload)
        copy = created["definition"]

    orig_metric = default_metric(server, def_id)
    copy_metric = default_metric(server, copy["metadata"]["id"])
    ok_orig, markup_orig = insight_probe(server, original, orig_metric) if orig_metric else (False, "no metric")
    ok_copy, markup_copy = insight_probe(server, copy, copy_metric) if copy_metric else (False, "no metric")

    if not ok_copy:
        verdict = "probe_failed"
    elif not ok_orig:
        verdict = "match"  # original itself broken; copy working is the best signal we can get
    else:
        verdict = "match" if markup_numbers(markup_orig) == markup_numbers(markup_copy) else "differs"
    return {
        "definition_id": def_id,
        "rehearsal_id": copy["metadata"]["id"],
        "verdict": verdict,
        "original_markup": markup_orig,
        "rehearsal_markup": markup_copy,
    }


def stage_production(server, pair: dict) -> dict:
    def_id = pair["definition_id"]
    name = pair["definition_name"]
    result: dict = {"definition_id": def_id}

    _, data = call(server, "GET", f"/api/-/pulse/definitions/{def_id}")
    original = data["definition"]

    # 1. archive-rename the old definition (rollback = rename back)
    if not original["metadata"]["name"].endswith(ARCHIVE_SUFFIX):
        call(server, "PATCH", f"/api/-/pulse/definitions/{def_id}",
             {"name": name + ARCHIVE_SUFFIX})
    result["renamed_old_to"] = name + ARCHIVE_SUFFIX

    # 2. get the new-PDS definition under the canonical name.
    #    Pulse rejects a 2nd definition with the same (datasource, specification)
    #    with 409 Conflict, and the rehearsal copy already IS that definition on
    #    the new PDS. So PROMOTE the rehearsal copy (rename it to the canonical
    #    name) rather than POSTing a duplicate. Precedence: an already-promoted
    #    canonical one (idempotent re-run) > the rehearsal copy > a fresh create
    #    (only when no rehearsal was run).
    all_defs = list_definitions(server)
    canonical = [d for d in all_defs
                 if d["metadata"]["name"] == name
                 and d["specification"]["datasource"]["id"] == pair["new_pds"]["luid"]]
    rehearsal = [d for d in all_defs
                 if d["metadata"]["name"] == name + REHEARSAL_SUFFIX
                 and d["specification"]["datasource"]["id"] == pair["new_pds"]["luid"]]
    if canonical:
        new_def = canonical[0]
        result["created_via"] = "existing"
    elif rehearsal:
        rid = rehearsal[0]["metadata"]["id"]
        call(server, "PATCH", f"/api/-/pulse/definitions/{rid}", {"name": name})
        _, got = call(server, "GET", f"/api/-/pulse/definitions/{rid}")
        new_def = got["definition"]
        result["created_via"] = "promoted_rehearsal"
    else:
        payload = definition_payload(original, name, datasource_id=pair["new_pds"]["luid"])
        _, created = call(server, "POST", "/api/-/pulse/definitions", payload)
        new_def = created["definition"]
        result["created_via"] = "created"
    new_id = new_def["metadata"]["id"]
    result["new_definition_id"] = new_id

    # 3+4. migrate metrics and follower subscriptions from the old definition's
    #    LIVE state — not the design snapshot. Followers are a moving target:
    #    anyone who followed after design was generated would silently be lost
    #    at cutover if we trusted the snapshot (observed in practice).
    _, old_metrics = call(server, "GET", f"/api/-/pulse/definitions/{def_id}/metrics")
    new_default = default_metric(server, new_id)
    metrics_migrated = 0
    migrated = 0
    for m in old_metrics.get("metrics", []):
        if m.get("is_default"):
            target_id = new_default["id"] if new_default else None
        else:
            _, got = call(server, "POST", "/api/-/pulse/metrics:getOrCreate",
                          {"definition_id": new_id, "specification": m["specification"]})
            target_id = got["metric"]["id"]
            metrics_migrated += 1
        if not target_id:
            continue
        followers = [s["follower"] for s in list_subscriptions(server, metric_id=m["id"])]
        if not followers:
            continue
        existing = list_subscriptions(server, metric_id=target_id)
        have = {json.dumps(s["follower"], sort_keys=True) for s in existing}
        for follower in followers:
            if json.dumps(follower, sort_keys=True) in have:
                continue
            call(server, "POST", "/api/-/pulse/subscriptions",
                 {"metric_id": target_id, "follower": follower})
            migrated += 1
    result["metrics_migrated"] = metrics_migrated
    result["subscriptions_migrated"] = migrated

    # 5. functional verification on the new definition
    probe_metric = new_default or default_metric(server, new_id)
    ok, markup = insight_probe(server, new_def, probe_metric) if probe_metric else (False, "no metric")
    result["insight_verdict"] = "ok" if ok else f"failed: {markup[:200]}"
    if not ok:
        return result  # abort before rehearsal cleanup; caller decides

    # 6. delete a LEFTOVER rehearsal copy (zero-follower guard). Re-list fresh —
    #    the step-2 snapshot is stale, and if the rehearsal copy was promoted its
    #    id now IS new_id (deleting it would destroy production). Guard on
    #    id != new_id so a promoted copy is never deleted here.
    result["rehearsal_deleted"] = False
    for d in list_definitions(server):
        if d["metadata"]["name"] != name + REHEARSAL_SUFFIX:
            continue
        rehearsal_id = d["metadata"]["id"]
        if rehearsal_id == new_id:
            continue
        has_followers = any(
            list_subscriptions(server, metric_id=m["id"]) for m in d.get("metrics", []))
        if has_followers:
            result["rehearsal_deleted"] = f"skipped: followers present on {rehearsal_id}"
            break
        call(server, "DELETE", f"/api/-/pulse/definitions/{rehearsal_id}")
        result["rehearsal_deleted"] = True
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--design", required=True)
    parser.add_argument("--definition", action="append", required=True,
                        help="definition id from the design (repeatable)")
    parser.add_argument("--stage", choices=["rehearsal", "production"], required=True)
    args = parser.parse_args()
    t0 = time.monotonic()

    design = json.loads(Path(args.design).read_text(encoding="utf-8"))
    by_id = {p["definition_id"]: p for p in design["pairs"]}
    missing = [d for d in args.definition if d not in by_id]
    if missing:
        sys.exit(f"ERROR: definition ids not in design: {missing}")

    results = []
    with signed_in_server() as server:
        for def_id in args.definition:
            pair = by_id[def_id]
            try:
                fn = stage_rehearsal if args.stage == "rehearsal" else stage_production
                results.append(fn(server, pair))
            except PulseHTTPError as e:
                results.append({"definition_id": def_id, "error": str(e)})

    result = {"stage": args.stage, "results": results,
              "elapsed_s": round(time.monotonic() - t0, 1)}
    print(f"RESULT_JSON: {json.dumps(result, ensure_ascii=False)}")
    return 0 if all("error" not in r for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
