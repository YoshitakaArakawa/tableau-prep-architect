#!/usr/bin/env python3
"""Extract Tableau Server/Cloud project structure for the deploy target.

Read-only. Resolves a user-specified project (by path or LUID) and writes a
`deploy-context.md` describing:

  - Target parent project (path, LUID, writeable?)
  - Subprojects directly under the target (incl. stg/intermediate/marts presence)
  - Existing flows in the target subtree (for naming-collision awareness in decompose)
  - Layers missing under the target (consumed by prep-deployer preflight)

Why path/LUID and not URL?
  Tableau Cloud project URLs use vizportalUrlId (numeric), which is NOT exposed
  via the standard REST API. URL -> LUID requires Metadata API or manual lookup.
  This script accepts the project name, "Parent/Child" path, or LUID.

Usage:
    python get_project_structure.py --project-path "Sales Analytics" \\
        -o work/<date>/deploy-context.md

    python get_project_structure.py --project-path "99_Old/Sample Project" \\
        -o work/<date>/deploy-context.md

    python get_project_structure.py --project-id 12345-abcde \\
        -o work/<date>/deploy-context.md
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))

import tableauserverclient as TSC  # noqa: E402

from tableau_auth import sign_in_server  # noqa: E402


DBT_LAYERS = ("stg", "intermediate", "marts")


def parse_args():
    p = argparse.ArgumentParser(description="Extract project structure for deploy target")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--project-path",
                     help="Project name or 'Parent/Child' path (case-sensitive)")
    grp.add_argument("--project-id", help="Project LUID")
    p.add_argument("-o", "--output", required=True,
                   help="Output deploy-context.md path")
    return p.parse_args()


def fetch_all_projects(server):
    req = TSC.RequestOptions()
    req.pagesize = 1000
    all_items = []
    page = 1
    while True:
        req.pagenumber = page
        items, pag = server.projects.get(req)
        all_items.extend(items)
        if pag.page_number * pag.page_size >= pag.total_available:
            break
        page += 1
    return all_items


def resolve_path(projects, path: str):
    """Walk parent_id chain segment by segment to find the deepest existing prefix.

    Returns:
        target:           ProjectItem of the full path, or None if any segment is missing
        existing_chain:   list of ProjectItems for segments that exist, root-first
                          (empty list if the very first segment does not exist)
        pending_segments: list of segment names not yet created, in order
        status:           "exists" (no pending) | "pending" (>=1 pending)

    The deepest existing project is `existing_chain[-1]` if non-empty, else None
    (meaning all pending segments would start at top-level).

    The model intentionally allows arbitrary-depth pending creation. Each pending
    segment is created by prep-deployer's preflight with a separate user approval.

    Raises ValueError only on ambiguity (multiple matches at some level).
    """
    segments = [s.strip() for s in path.split("/") if s.strip()]
    if not segments:
        raise ValueError("empty path")

    existing_chain = []
    parent_id = None  # None means "top-level"
    for i, seg in enumerate(segments):
        candidates = [p for p in projects
                      if p.parent_id == parent_id and p.name == seg]
        if len(candidates) > 1:
            traversed = "/".join(segments[:i + 1])
            hint = ("Use --project-id to disambiguate."
                    if i == len(segments) - 1
                    else "Disambiguate the path by specifying intermediate names more precisely.")
            raise ValueError(
                f"ambiguous segment '{seg}' at path position {i + 1} "
                f"(matched {len(candidates)} projects at '{traversed}'). {hint}")
        if not candidates:
            pending = segments[i:]
            return None, existing_chain, pending, "pending"
        existing_chain.append(candidates[0])
        parent_id = candidates[0].id

    target = existing_chain[-1]
    return target, existing_chain, [], "exists"


def project_path(projects, project):
    by_id = {p.id: p for p in projects}
    segments = [project.name]
    cur = project
    while cur.parent_id:
        cur = by_id.get(cur.parent_id)
        if cur is None:
            segments.append("?")
            break
        segments.append(cur.name)
    return "/".join(reversed(segments))


def fetch_flows_in_subtree(server, project_ids):
    """Return list of (project_id, FlowItem) for flows in any of the given projects."""
    req = TSC.RequestOptions()
    req.pagesize = 1000
    flows = []
    page = 1
    while True:
        req.pagenumber = page
        items, pag = server.flows.get(req)
        flows.extend(items)
        if pag.page_number * pag.page_size >= pag.total_available:
            break
        page += 1
    return [(f.project_id, f) for f in flows if f.project_id in project_ids]


def check_writeable(server, project) -> str:
    """Best-effort: report ProjectItem.writeable if populated; else 'unknown'."""
    try:
        # Some sites/PATs populate this; many don't via TSC. Re-fetch by id.
        fresh = server.projects.get_by_id(project.id)
        val = getattr(fresh, "_writeable", None)
        if val is True:
            return "yes"
        if val is False:
            return "no"
    except Exception:
        pass
    return "unknown (TSC does not populate this for all PATs; run a dry create_projects to confirm)"


def render(target, target_path, target_status, target_writeable,
           existing_chain, existing_prefix_path, pending_segments,
           server_url, site_name,
           children, flows_in_subtree, all_projects):
    """Render deploy-context.md.

    Model: the "target" is the direct parent of stg/int/marts at the end of
    --project-path. Above it, any number of intermediate segments are allowed,
    and any trailing run of those (including the target itself) may not exist
    yet — captured as pending_segments. prep-deployer's preflight creates them
    one at a time, each requiring user approval.
    """
    by_id = {p.id: p for p in all_projects}
    layer_status = {layer: None for layer in DBT_LAYERS}
    if target is not None:
        for c in children:
            if c.name in layer_status:
                layer_status[c.name] = c

    missing = [layer for layer, c in layer_status.items() if c is None]
    deepest_existing = existing_chain[-1] if existing_chain else None

    lines = []
    lines.append("---")
    lines.append(f"generated_at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    lines.append("generated_by: prep-extractor/get_project_structure.py")
    lines.append(f"server: {server_url}")
    lines.append(f"site: {site_name or '<default>'}")
    lines.append(f"target_path: {target_path}")
    lines.append(f"target_status: {target_status}")
    lines.append(f"target_luid: {target.id if target else 'null'}")
    lines.append(f"existing_prefix_path: {existing_prefix_path if existing_prefix_path else 'null'}")
    lines.append(f"existing_prefix_luid: {deepest_existing.id if deepest_existing else 'null'}")
    lines.append("pending_segments:")
    if pending_segments:
        for seg in pending_segments:
            lines.append(f"  - {seg}")
    else:
        lines.append("  []")
    lines.append("---")
    lines.append("")
    lines.append("# deploy-context")
    lines.append("")
    lines.append("Read-only snapshot of the Tableau Server/Cloud structure under the deploy target.")
    lines.append("Consumed by prep-architect (decompose) and prep-deployer (preflight + publish).")
    lines.append("")
    lines.append("Model: **target** = the direct parent of `stg / intermediate / marts`. ")
    lines.append("Above the target, any number of intermediate path segments are allowed. ")
    lines.append("Trailing segments may be `pending`; prep-deployer preflight creates them one ")
    lines.append("at a time, each requiring user approval, then creates the dbt layers.")
    lines.append("")

    lines.append("## Target (parent of stg/int/marts)")
    lines.append("")
    lines.append(f"- Server: `{server_url}`")
    lines.append(f"- Site: `{site_name or '<default>'}`")
    lines.append(f"- Path: `{target_path}`")
    if target is not None:
        lines.append(f"- LUID: `{target.id}`")
        lines.append(f"- Status: **exists**")
        lines.append(f"- Writeable by current PAT: {target_writeable}")
    else:
        lines.append(f"- LUID: _(pending — not created yet)_")
        lines.append(f"- Status: **pending** — prep-deployer preflight will request approval to create the missing segments.")
    lines.append("")

    lines.append("## Existing prefix")
    lines.append("")
    if not existing_chain:
        lines.append("_(no segments of the target path exist yet — every segment is pending, including the top-level.)_")
        lines.append("")
        lines.append("**Caution**: top-level project creation has org governance implications. ")
        lines.append("Confirm with the user that creating the first segment at top-level is intended.")
    else:
        lines.append("Deepest existing project (= where the first pending segment will be created):")
        lines.append("")
        lines.append(f"- Path: `{existing_prefix_path}`")
        lines.append(f"- LUID: `{deepest_existing.id}`")
        lines.append("")
        lines.append("Full chain (root → leaf):")
        lines.append("")
        lines.append("| depth | name | LUID |")
        lines.append("|---|---|---|")
        for depth, p in enumerate(existing_chain, start=1):
            lines.append(f"| {depth} | `{p.name}` | `{p.id}` |")
    lines.append("")

    lines.append("## Pending segments")
    lines.append("")
    if not pending_segments:
        lines.append("_(no pending segments — target exists.)_")
    else:
        lines.append("Segments to create, in order. Each requires user approval at preflight time.")
        lines.append("")
        lines.append("| order | name | parent at creation time |")
        lines.append("|---|---|---|")
        prev = existing_prefix_path or "<top-level>"
        for i, seg in enumerate(pending_segments, start=1):
            lines.append(f"| {i} | `{seg}` | `{prev}` |")
            prev = f"{prev}/{seg}" if prev != "<top-level>" else seg
    lines.append("")

    lines.append("## Subprojects directly under target")
    lines.append("")
    if target is None:
        lines.append("_(target does not exist yet — N/A.)_")
    elif not children:
        lines.append("_(none)_")
    else:
        lines.append("| name | LUID | dbt layer? |")
        lines.append("|---|---|---|")
        for c in sorted(children, key=lambda x: x.name or ""):
            layer_mark = "★" if c.name in DBT_LAYERS else ""
            lines.append(f"| `{c.name}` | `{c.id}` | {layer_mark} |")
    lines.append("")

    lines.append("## dbt layer presence")
    lines.append("")
    if target is None:
        lines.append("_(target does not exist yet — all 3 layers will be created after the target is created.)_")
    else:
        lines.append("| layer | present? | LUID |")
        lines.append("|---|---|---|")
        for layer in DBT_LAYERS:
            c = layer_status[layer]
            if c:
                lines.append(f"| `{layer}` | yes | `{c.id}` |")
            else:
                lines.append(f"| `{layer}` | **NO** | — |")
        lines.append("")
        if missing:
            lines.append(f"**Missing layers**: {', '.join(f'`{m}`' for m in missing)}. "
                         "prep-deployer preflight will request approval to create these.")
        else:
            lines.append("All standard layers exist. prep-deployer can skip the dbt-layer creation step.")
    lines.append("")

    lines.append("## Existing flows in target subtree")
    lines.append("")
    if target is None:
        lines.append("_(target does not exist yet — no flows possible.)_")
    elif not flows_in_subtree:
        lines.append("_(no flows under target — name collisions impossible.)_")
    else:
        lines.append("Use this to avoid name collisions when decompose names new .tfl files.")
        lines.append("")
        lines.append("| project | flow name | LUID |")
        lines.append("|---|---|---|")
        for proj_id, f in sorted(flows_in_subtree,
                                  key=lambda x: (by_id[x[0]].name if x[0] in by_id else "",
                                                 x[1].name or "")):
            proj_name = by_id[proj_id].name if proj_id in by_id else "?"
            lines.append(f"| `{proj_name}` | `{f.name}` | `{f.id}` |")
    lines.append("")

    lines.append("## Next step")
    lines.append("")
    lines.append("Pass this file's path to:")
    lines.append("")
    lines.append("- **prep-architect (decompose)** — uses `## Existing flows` to avoid name collisions.")
    lines.append("- **prep-deployer (preflight)** — iterates `pending_segments` (creating each under ")
    lines.append("  the previous, with user approval), then ensures `stg / intermediate / marts` exist ")
    lines.append("  under the resulting target.")
    return "\n".join(lines) + "\n"


def main():
    args = parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    server, auth = sign_in_server()
    with server.auth.sign_in(auth):
        all_projects = fetch_all_projects(server)
        print(f"[info] fetched {len(all_projects)} projects from server", file=sys.stderr)

        target = None
        target_status = "exists"
        existing_chain = []
        pending_segments = []

        if args.project_id:
            try:
                target = server.projects.get_by_id(args.project_id)
            except Exception as e:
                sys.exit(f"ERROR: cannot fetch project_id '{args.project_id}': {e}")
            # Reconstruct the existing_chain by walking parents up.
            chain_rev = [target]
            cur = target
            while cur.parent_id:
                parent = next((p for p in all_projects if p.id == cur.parent_id), None)
                if parent is None:
                    break
                chain_rev.append(parent)
                cur = parent
            existing_chain = list(reversed(chain_rev))
        else:
            try:
                target, existing_chain, pending_segments, target_status = \
                    resolve_path(all_projects, args.project_path)
            except ValueError as e:
                sys.exit(f"ERROR: cannot resolve path '{args.project_path}': {e}")

        target_path = (project_path(all_projects, target)
                       if target is not None
                       else args.project_path)
        existing_prefix_path = (
            "/".join(p.name for p in existing_chain) if existing_chain else None
        )
        target_writeable = (
            check_writeable(server, target) if target is not None else "n/a (pending)"
        )

        if target is not None:
            children = [p for p in all_projects if p.parent_id == target.id]
            subtree_ids = {target.id}
            frontier = [target.id]
            while frontier:
                new_frontier = []
                for pid in frontier:
                    kids = [p.id for p in all_projects if p.parent_id == pid]
                    new_frontier.extend(kids)
                    subtree_ids.update(kids)
                frontier = new_frontier
            flows_in_subtree = fetch_flows_in_subtree(server, subtree_ids)
        else:
            children = []
            flows_in_subtree = []

        text = render(target, target_path, target_status, target_writeable,
                      existing_chain, existing_prefix_path, pending_segments,
                      server.server_address, server.site_id,
                      children, flows_in_subtree, all_projects)
        output.write_text(text, encoding="utf-8")
        print(f"Wrote deploy-context: {output}")
        print(f"  target:   {target_path}  ({target.id if target else 'PENDING'})")
        print(f"  status:   {target_status}")
        if existing_chain:
            print(f"  existing: {existing_prefix_path}  ({existing_chain[-1].id})")
        else:
            print(f"  existing: <none — pending starts at top-level>")
        if pending_segments:
            print(f"  pending:  {' → '.join(pending_segments)}  ({len(pending_segments)} segment(s))")
        if target is not None:
            missing = [layer for layer in DBT_LAYERS
                       if not any(c.name == layer for c in children)]
            if missing:
                print(f"  missing dbt layers: {', '.join(missing)}")
            else:
                print(f"  dbt layers: all present (stg/intermediate/marts)")
            print(f"  flows in subtree: {len(flows_in_subtree)}")
        else:
            print(f"  preflight will need to: create {len(pending_segments)} segment(s), "
                  "then create stg/int/marts under the last one")


if __name__ == "__main__":
    main()
