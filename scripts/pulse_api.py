"""Shared Pulse REST helpers for tableau-pulse-repointer scripts.

All Pulse endpoints live under the versionless path family `/api/-/pulse/...`
and accept the ordinary REST session token via `X-Tableau-Auth` (the repo's
OAuth PKCE token works as-is). See references/pulse-api-recipe.md for the
behavioural contract these helpers encode (pagination trap, FULL view, etc.).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

USER_AGENT = "tableau-prep-architect/tableau-pulse-repointer"

# The definitions list silently truncates at the server default (10). 100 keeps
# round-trips low while staying well under any URL/response limit.
PAGE_SIZE = 100


class PulseHTTPError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, body: str):
        super().__init__(f"{method} {path} -> HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


def call(server: Any, method: str, path: str, body: dict | None = None,
         ok: tuple[int, ...] = (200, 201, 204)) -> tuple[int, dict]:
    """One Pulse REST call. Raises PulseHTTPError on non-ok status."""
    url = server.server_address.rstrip("/") + path
    req = urllib.request.Request(
        url=url,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Tableau-Auth": server.auth_token,
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read().decode(errors="replace")
        if status in ok:
            return status, (json.loads(raw) if raw.strip() else {})
        raise PulseHTTPError(method, path, status, raw) from None


def list_definitions(server: Any) -> list[dict]:
    """All metric definitions, FULL view, following next_page_token to the end."""
    defs: list[dict] = []
    page_token = ""
    while True:
        path = f"/api/-/pulse/definitions?view=DEFINITION_VIEW_FULL&page_size={PAGE_SIZE}"
        if page_token:
            path += f"&page_token={urllib.parse.quote(page_token)}"
        _, data = call(server, "GET", path)
        defs.extend(data.get("definitions", []))
        page_token = data.get("next_page_token", "")
        if not page_token:
            return defs


def list_subscriptions(server: Any, metric_id: str | None = None) -> list[dict]:
    """Site-wide (or per-metric) Pulse subscriptions, paginated."""
    subs: list[dict] = []
    page_token = ""
    while True:
        params = [f"page_size={PAGE_SIZE}"]
        if metric_id:
            params.append(f"metric_id={metric_id}")
        if page_token:
            params.append(f"page_token={urllib.parse.quote(page_token)}")
        _, data = call(server, "GET", "/api/-/pulse/subscriptions?" + "&".join(params))
        subs.extend(data.get("subscriptions", []))
        page_token = data.get("next_page_token", "")
        if not page_token:
            return subs


def definition_payload(source: dict, name: str, datasource_id: str | None = None) -> dict:
    """Create-payload from a FULL-view definition; optionally swap the datasource.

    The viz_state is copied verbatim: the embedded sqlproxy label is inert at
    query time (specification.datasource.id is the source of truth), so no
    rewrite is needed or attempted.
    """
    spec = json.loads(json.dumps(source["specification"]))
    if datasource_id:
        spec["datasource"]["id"] = datasource_id
    return {
        "name": name,
        "description": source.get("metadata", {}).get("description", ""),
        "specification": spec,
        "extension_options": source.get("extension_options", {}),
        "representation_options": source.get("representation_options", {}),
        "insights_options": source.get("insights_options", {}),
        "comparisons": source.get("comparisons", {}),
    }


def extract_referenced_fields(definition: dict) -> list[str]:
    """Field names the definition's specification references (for parity review).

    Sources: viz_state fieldCaption entries, basic_specification measure /
    time_dimension / filters, and extension_options.allowed_dimensions. Field
    mismatches only surface at insight time (400), so these are surfaced for
    human review in the runbook.
    """
    fields: set[str] = set()
    spec = definition.get("specification", {})
    basic = spec.get("basic_specification") or {}
    if basic:
        if basic.get("measure", {}).get("field"):
            fields.add(basic["measure"]["field"])
        if basic.get("time_dimension", {}).get("field"):
            fields.add(basic["time_dimension"]["field"])
        for f in basic.get("filters", []):
            if f.get("field"):
                fields.add(f["field"])
    viz_string = (spec.get("viz_state_specification") or {}).get("viz_state_string", "")
    if viz_string:
        try:
            viz = json.loads(viz_string)
            for shelf in ("rows", "columns", "filters"):
                for entry in (viz.get("vizState") or {}).get(shelf, []):
                    if entry.get("fieldCaption"):
                        fields.add(entry["fieldCaption"])
        except json.JSONDecodeError:
            pass
    for dim in (definition.get("extension_options") or {}).get("allowed_dimensions", []):
        fields.add(dim)
    return sorted(fields)


def insight_probe(server: Any, definition: dict, metric: dict) -> tuple[bool, str]:
    """Generate a BAN insight for (definition, metric). Returns (ok, markup_or_error).

    A 400 here is the field-mismatch signal: create never validates fields, so
    this probe is the only machine check that a definition works on its PDS.
    """
    spec = definition["specification"]
    definition_input: dict = {
        "datasource": spec["datasource"],
        "is_running_total": spec.get("is_running_total", False),
    }
    for key in ("viz_state_specification", "basic_specification"):
        if spec.get(key):
            definition_input[key] = spec[key]
    bundle = {
        "bundle_request": {
            "version": 1,
            "options": {"output_format": "OUTPUT_FORMAT_TEXT", "time_zone": "Asia/Tokyo"},
            "input": {
                "metadata": {
                    "name": definition["metadata"]["name"],
                    "metric_id": metric["id"],
                    "definition_id": definition["metadata"]["id"],
                },
                "metric": {
                    "definition": definition_input,
                    "metric_specification": metric["specification"],
                    "extension_options": definition.get("extension_options", {}),
                    "representation_options": definition.get("representation_options", {}),
                    "insights_options": definition.get("insights_options", {}),
                },
            },
        }
    }
    try:
        _, data = call(server, "POST", "/api/-/pulse/insights/ban", bundle)
    except PulseHTTPError as e:
        return False, f"HTTP {e.status}: {e.body[:200]}"
    for group in (data.get("bundle_response", {}).get("result", {}).get("insight_groups") or []):
        for insight in group.get("insights", []):
            markup = insight.get("result", {}).get("markup", "")
            if markup:
                return True, markup
    return True, "(201 but no markup in bundle_response)"
