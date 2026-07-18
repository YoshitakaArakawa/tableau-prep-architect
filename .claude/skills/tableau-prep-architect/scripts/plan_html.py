"""HTML render target for the Stop-2 decomposition review.

`render_plan_md.py` calls `render_html()` AFTER `validate_plan_with_source`
passes, so the HTML shows the same validated design the markdown shows and
the builder builds — one validation gate, two views (md = git-tracked design
record / html = visual review surface the user opens in a browser).

Everything is static: no JavaScript, no external resources; layer-colored SVG
DAGs are computed here (columns = longest-path depth, rows = parent
barycenter). Tooltips ride on <title>/title attributes only.

Sections:
  - As-is フロー → 分解先マップ: step strip (every original step, color =
    destination layer; red = unassigned) + full node-level As-is DAG with the
    destination flow printed on each node
  - 依存 DAG (分解後): sources → stg → int → marts → 元 Output (置換)
    (dashed edges show which original PDS each mart replaces — the
    comparator pairing)
  - New .tfl files cards / Output mapping / Target project layout /
    Alternatives
"""
from __future__ import annotations

import html
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from flow_io import inspect_input_node  # noqa: E402
from plan_model import StepResolver  # noqa: E402

LAYERS = ["staging", "intermediate", "marts"]
LAYER_LABEL = {"staging": "stg", "intermediate": "int", "marts": "marts"}
LAYER_COL = {"source": 0, "staging": 1, "intermediate": 2, "marts": 3, "rep": 4}
COL_LABEL = ["Sources", "staging", "intermediate", "marts", "元 Output (置換)"]

# After-DAG geometry
COL_W, BOX_W, BOX_H, V_GAP, TOP, LEFT = 250, 210, 46, 22, 70, 30
# As-is DAG geometry
NB_W, NB_H, NB_GAPX, NB_GAPY = 152, 40, 42, 16


def esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""))


def trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------------------
# source topology (from the resolver — same BFS numbering as everything else)
# ---------------------------------------------------------------------------

def source_steps(resolver: StepResolver) -> dict[int, dict[str, Any]]:
    steps: dict[int, dict[str, Any]] = {}
    for uuid in resolver.order:
        node = resolver.flow["nodes"][uuid]
        s = resolver.step_by_uuid[uuid]
        steps[s] = {
            "name": node.get("name") or "?",
            "type": (node.get("nodeType") or "?").split(".")[-1],
            "base": node.get("baseType"),
            "pds": node.get("datasourceName"),  # PublishExtract only
            "next": [resolver.step_by_uuid[nx["nextNodeId"]]
                     for nx in node.get("nextNodes") or []
                     if nx.get("nextNodeId") in resolver.step_by_uuid],
        }
    return steps


# ---------------------------------------------------------------------------
# assignment: original step -> destination(s)
# ---------------------------------------------------------------------------

def compute_assignment(plan: dict, steps: dict[int, dict]) -> dict[int, dict]:
    targets: dict[int, list[dict]] = defaultdict(list)
    for f in plan["flows"]:
        fl, ly = f["name"], f["layer"]
        if f["kind"] == "pds_augment":
            targets[f["source_input_step"]].append(
                {"flow": fl, "layer": ly, "role": "Live PDS 化 (augment)"})
            continue
        for s in f.get("included_steps") or []:
            targets[s].append({"flow": fl, "layer": ly, "role": "転写"})
        for sp in f.get("splits") or []:
            targets[sp["step"]].append(
                {"flow": fl, "layer": ly,
                 "role": f"split (actions {sp['action_indices']})"})
        for inp in f.get("inputs") or []:
            if inp["kind"] == "transplant":
                targets[inp["step"]].append(
                    {"flow": fl, "layer": ly, "role": "Input 転写 (transplant)"})
            elif inp["kind"] == "passthrough_pds":
                for s in inp.get("replaces_steps") or []:
                    targets[s].append(
                        {"flow": None, "layer": "pass",
                         "role": f"passthrough — 既存 PDS {inp['pds_name']} をそのまま参照"})

    assign: dict[int, dict] = {}
    for s, node in steps.items():
        tg = targets.get(s, [])
        real = [t for t in tg if t["flow"]]
        if real:
            assign[s] = {"cat": "flow", "targets": real}
        elif any(t["layer"] == "pass" for t in tg):
            assign[s] = {"cat": "pass", "targets": tg}
        elif node["base"] == "output":
            assign[s] = {"cat": "replaced",
                         "targets": [{"flow": None, "layer": "rep",
                                      "role": "元 Output — 分解後 flow の publish が置換"}]}
        else:
            assign[s] = {"cat": "orphan",
                         "targets": [{"flow": None, "layer": "del",
                                      "role": "どの新フローにも未割当 — 削除候補 (Stop 2 で要確認)"}]}
    return assign


def counts_line(assign: dict) -> str:
    c: dict[str, int] = defaultdict(int)
    for a in assign.values():
        if a["cat"] == "flow":
            for t in a["targets"]:
                c[t["layer"]] += 1
        else:
            c[a["cat"]] += 1
    bits = []
    for ly in LAYERS:
        if c[ly]:
            bits.append(f'<span class="scount b-{ly}">{LAYER_LABEL[ly]} <b>{c[ly]}</b></span>')
    if c["replaced"]:
        bits.append(f'<span class="scount b-rep">元 Output 置換 <b>{c["replaced"]}</b></span>')
    if c["pass"]:
        bits.append(f'<span class="scount b-rep">passthrough <b>{c["pass"]}</b></span>')
    if c["orphan"]:
        bits.append(f'<span class="scount b-del">未割当 (削除候補) <b>{c["orphan"]}</b></span>')
    return "".join(bits)


def strip_html(steps: dict, assign: dict) -> str:
    cells = []
    for s in sorted(steps):
        node, a = steps[s], assign[s]
        tips = "; ".join((f"→ {t['flow']} ({t['role']})" if t["flow"] else t["role"])
                         for t in a["targets"])
        title = f"#{s} {node['name']} {tips}"
        if a["cat"] == "flow":
            lys = [t["layer"] for t in a["targets"]]
            if len(set(lys)) > 1:
                v1, v2 = f"var(--{LAYER_LABEL[lys[0]]})", f"var(--{LAYER_LABEL[lys[1]]})"
                style = f"background:linear-gradient(135deg,{v1} 50%,{v2} 50%);color:#fff"
            else:
                style = f"background:var(--{LAYER_LABEL[lys[0]]});color:#fff"
            cls = "cell"
        elif a["cat"] == "pass":
            style, cls = "", "cell cell-pass"
        elif a["cat"] == "replaced":
            style, cls = "", "cell cell-rep"
        else:
            style, cls = "", "cell cell-del"
        cells.append(f'<div class="{cls}" style="{style}" title="{esc(title)}">{s}</div>')
    return '<div class="strip">' + "".join(cells) + "</div>"


# ---------------------------------------------------------------------------
# As-is full DAG
# ---------------------------------------------------------------------------

def edge_path(x1, y1, x2, y2, style="") -> str:
    mx = (x1 + x2) / 2
    cls = "edge dashed" if style == "dashed" else "edge"
    return (f'<path d="M {x1:.0f} {y1:.0f} C {mx:.0f} {y1:.0f} '
            f'{mx:.0f} {y2:.0f} {x2:.0f} {y2:.0f}" class="{cls}"/>')


def svg_asis_full(plan: dict, steps: dict, assign: dict) -> str:
    """Node-level As-is DAG, left-to-right like Prep draws flows. Column =
    longest-path depth; rows ordered by parent barycenter (fewer crossings).
    Line 1 = #step name, line 2 = destination — readable without hover."""
    order = sorted(steps)
    parents: dict[int, list[int]] = defaultdict(list)
    for s in order:
        for nx in steps[s]["next"]:
            parents[nx].append(s)
    indeg = {s: len(parents[s]) for s in order}
    depth = {s: 0 for s in order}
    q = [s for s in order if indeg[s] == 0]
    while q:
        u = q.pop()
        for v in steps[u]["next"]:
            depth[v] = max(depth[v], depth[u] + 1)
            indeg[v] -= 1
            if indeg[v] == 0:
                q.append(v)
    cols: dict[int, list[int]] = defaultdict(list)
    for s in order:
        cols[depth[s]].append(s)
    rowpos: dict[int, float] = {}
    for c in sorted(cols):
        if c == 0:
            cols[c].sort()
        else:
            cols[c].sort(key=lambda s: (
                sum(rowpos.get(p, 0) for p in parents[s]) / len(parents[s])
                if parents[s] else 0, s))
        for i, s in enumerate(cols[c]):
            rowpos[s] = i
    pitch = NB_W + NB_GAPX
    pos = {s: (LEFT + depth[s] * pitch, 24 + rowpos[s] * (NB_H + NB_GAPY))
           for s in order}
    ncols = max(cols) + 1
    width = LEFT + (ncols - 1) * pitch + NB_W + 24
    height = 24 + max(len(v) for v in cols.values()) * (NB_H + NB_GAPY) + 10

    def dest_line(s: int) -> str:
        a = assign[s]
        if a["cat"] == "flow":
            return " + ".join(sorted({t["flow"] for t in a["targets"]}))
        if a["cat"] == "pass":
            return "passthrough (PDS 維持)"
        if a["cat"] == "replaced":
            return f"置換 → {steps[s].get('pds') or steps[s]['name']}"
        return "未割当 (削除候補)"

    def cls(s: int) -> str:
        a = assign[s]
        if a["cat"] == "flow":
            lys = {t["layer"] for t in a["targets"]}
            return "n-" + (sorted(lys)[0] if len(lys) == 1 else "split")
        return {"pass": "n-source", "replaced": "n-rep", "orphan": "n-del"}[a["cat"]]

    # natural pixel size + horizontal scroll (scaling down to container width
    # would make labels unreadable on deep flows)
    parts = [f'<svg viewBox="0 0 {width:.0f} {height:.0f}" '
             f'width="{width:.0f}" height="{height:.0f}" class="dag dag-asis" '
             f'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="As-is DAG">']
    for s in order:
        x1, y1 = pos[s]
        for nx in steps[s]["next"]:
            x2, y2 = pos[nx]
            parts.append(edge_path(x1 + NB_W, y1 + NB_H / 2, x2, y2 + NB_H / 2))
    for s in order:
        x, y = pos[s]
        node, a = steps[s], assign[s]
        tip = "; ".join((f"→ {t['flow']} ({t['role']})" if t["flow"] else t["role"])
                        for t in a["targets"])
        parts.append(f'<g class="node {cls(s)}"><title>#{s} {esc(node["name"])} '
                     f'[{esc(node["type"])}] {esc(tip)}</title>')
        parts.append(f'<rect x="{x:.0f}" y="{y:.0f}" width="{NB_W}" height="{NB_H}" rx="7"/>')
        parts.append(f'<text x="{x+NB_W/2:.0f}" y="{y+16:.0f}" text-anchor="middle" '
                     f'class="nlabel">{esc(trunc("#" + str(s) + " " + node["name"], 24))}</text>')
        parts.append(f'<text x="{x+NB_W/2:.0f}" y="{y+31:.0f}" text-anchor="middle" '
                     f'class="nsub">{esc(trunc(dest_line(s), 27))}</text>')
        parts.append("</g>")
    parts.append("</svg>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# After DAG (decomposed flows + replaced-original column)
# ---------------------------------------------------------------------------

def build_after_graph(plan: dict, resolver: StepResolver):
    """Nodes/edges of the decomposed dependency DAG. Edge style "dashed" =
    mart -> original output PDS it replaces (the comparator pairing)."""
    flows = plan["flows"]
    by_name = {f["name"]: f for f in flows}
    nodes: dict[str, dict] = {}
    edges: list[tuple[str, str, str]] = []

    for f in flows:
        nodes[f["name"]] = {"label": f["name"], "col": LAYER_COL[f["layer"]],
                            "layer": f["layer"]}

    def ext(nid: str, label: str, layer="source", col=0):
        if nid not in nodes:
            nodes[nid] = {"label": label, "col": col, "layer": layer}

    for f in flows:
        fid = f["name"]
        orig = f.get("source_original_output_name")
        if orig:
            rid = "rep::" + orig
            ext(rid, f"{orig}\n(分解後 PDS が置換)", layer="rep", col=4)
            edges.append((fid, rid, "dashed"))
        if f["kind"] == "pds_augment":
            info = inspect_input_node(resolver.flow,
                                      resolver.uuid(f["source_input_step"]))
            cap = info.get("vconn_caption") or "source"
            sid = "src::" + cap
            ext(sid, f"{cap}\n(仮想接続)")
            edges.append((sid, fid, ""))
            continue
        if f.get("input_status") == "needs_provisioning":
            prov = f.get("provisioning") or {}
            sid = "src::prov:" + fid
            ext(sid, f"{prov.get('source', '未整備 source')}\n(needs_provisioning)")
            edges.append((sid, fid, "dashed"))
            continue
        for inp in f.get("inputs", []) or []:
            k = inp["kind"]
            if k == "upstream_pds" and inp["pds_name"] in by_name:
                edges.append((inp["pds_name"], fid, ""))
            elif k == "passthrough_pds":
                sid = "src::" + inp["pds_name"]
                ext(sid, f"{inp['pds_name']}\n(passthrough PDS)")
                edges.append((sid, fid, ""))
            elif k == "transplant":
                node = resolver.node(inp["step"])
                sid = f"src::step{inp['step']}"
                ext(sid, f"{node.get('name') or '元 Input'}\n(元 Input #{inp['step']} transplant)")
                edges.append((sid, fid, ""))
    return nodes, edges


def svg_after(nodes: dict, edges: list) -> str:
    cols: dict[int, list[str]] = defaultdict(list)
    for nid, n in nodes.items():
        cols[n["col"]].append(nid)
    ncols = max(cols) + 1 if cols else 1
    pos: dict[str, tuple[float, float]] = {}
    for c in range(ncols):
        for i, nid in enumerate(cols.get(c, [])):
            pos[nid] = (LEFT + c * COL_W, TOP + i * (BOX_H + V_GAP))
    height = TOP + max((len(v) for v in cols.values()), default=1) * (BOX_H + V_GAP) + 20
    width = LEFT + (ncols - 1) * COL_W + BOX_W + 30

    parts = [f'<svg viewBox="0 0 {width:.0f} {height:.0f}" class="dag" '
             f'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="After DAG">']
    for c in range(ncols):
        cx = LEFT + c * COL_W + BOX_W / 2
        parts.append(f'<text x="{cx:.0f}" y="36" class="colhdr" '
                     f'text-anchor="middle">{esc(COL_LABEL[c])}</text>')
    for src, dst, style in edges:
        if src in pos and dst in pos:
            x1, y1 = pos[src]
            x2, y2 = pos[dst]
            parts.append(edge_path(x1 + BOX_W, y1 + BOX_H / 2, x2, y2 + BOX_H / 2, style))
    for nid, n in nodes.items():
        x, y = pos[nid]
        parts.append(f'<g class="node n-{n["layer"]}">')
        parts.append(f'<rect x="{x:.0f}" y="{y:.0f}" width="{BOX_W}" height="{BOX_H}" rx="8"/>')
        lines = n["label"].split("\n")
        if len(lines) == 1:
            parts.append(f'<text x="{x+BOX_W/2:.0f}" y="{y+BOX_H/2+4:.0f}" '
                         f'text-anchor="middle" class="nlabel">{esc(trunc(lines[0], 32))}</text>')
        else:
            parts.append(f'<text x="{x+BOX_W/2:.0f}" y="{y+BOX_H/2-4:.0f}" '
                         f'text-anchor="middle" class="nlabel">{esc(trunc(lines[0], 32))}</text>')
            parts.append(f'<text x="{x+BOX_W/2:.0f}" y="{y+BOX_H/2+13:.0f}" '
                         f'text-anchor="middle" class="nsub">{esc(trunc(lines[1], 40))}</text>')
        parts.append("</g>")
    parts.append("</svg>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# entry cards
# ---------------------------------------------------------------------------

def card(f: dict, plan: dict, resolver: StepResolver) -> str:
    layer = f["layer"]
    rows = [f'<div class="kv"><span>kind</span><code>{esc(f["kind"])}</code></div>']
    if f.get("input_status") == "needs_provisioning":
        prov = f.get("provisioning") or {}
        rows.append('<div class="kv"><span>Input status</span>'
                    '<div><code>needs_provisioning</code> — build を skip</div></div>')
        rows.append(f'<div class="kv"><span>Source</span>'
                    f'<div>{esc(prov.get("source", "?"))} ({esc(prov.get("kind", "?"))})</div></div>')
        rows.append(f'<div class="kv"><span>推奨整備</span>'
                    f'<div>{esc(prov.get("recommendation", "—"))}</div></div>')
        desc = esc(f.get("description", ""))
        return (f'<div class="card c-{layer}">'
                f'<div class="card-h"><span class="badge b-{layer}">{esc(LAYER_LABEL[layer])}</span>'
                f'<h3>{esc(f["name"])}</h3></div>'
                f'<div class="card-b">{"".join(rows)}<p class="desc">{desc}</p></div></div>')
    if f["kind"] == "pds_augment":
        rows.append('<div class="kv"><span>Materialization</span><code>live_pds</code></div>')
    ins = []
    if f["kind"] == "pds_augment":
        info = inspect_input_node(resolver.flow, resolver.uuid(f["source_input_step"]))
        ins.append(f'仮想接続 <code>{esc(info.get("vconn_caption") or "?")}</code> / '
                   f'{esc(info.get("table_name") or "?")} '
                   f'(元 Input {esc(resolver.label(f["source_input_step"]))})')
    for inp in f.get("inputs", []) or []:
        if inp["kind"] == "upstream_pds":
            ins.append(f'PDS <code>{esc(inp["pds_name"])}</code> <span class="tag">upstream</span>')
        elif inp["kind"] == "passthrough_pds":
            ins.append(f'PDS <code>{esc(inp["pds_name"])}</code> '
                       f'<span class="tag pass">passthrough</span> '
                       f'<span class="muted">({esc(inp.get("project_path", "?"))})</span>')
        elif inp["kind"] == "transplant":
            ins.append(f'元 Input {esc(resolver.label(inp["step"]))} '
                       f'<span class="tag">transplant</span>')
    if ins:
        rows.append('<div class="kv"><span>Inputs</span><div class="stack">'
                    + "".join(f"<div>{x}</div>" for x in ins) + "</div></div>")
    out_name = (f.get("output") or {}).get("name") or f["name"]
    orig = f.get("source_original_output_name")
    out_html = f'<code>{esc(out_name)}</code>'
    if orig:
        out_html += f' <span class="tag orig">↦ 元 {esc(orig)}</span>'
    rows.append(f'<div class="kv"><span>Output</span><div>{out_html}</div></div>')
    for j in f.get("joins", []) or []:
        rows.append(f'<div class="kv"><span>Join</span><div>{esc(j)}</div></div>')
    tf = f.get("transforms") or []
    if tf:
        trows = "".join(
            f'<tr><td><code>{esc(t["op"])}</code></td><td><code>{esc(t["column_name"])}</code></td>'
            f'<td>{esc(t.get("to_caption") or "—")}</td><td>{esc(t.get("to_datatype") or "—")}</td></tr>'
            for t in tf)
        rows.append('<div class="kv"><span>Transforms</span><table class="mini">'
                    '<tr><th>op</th><th>column</th><th>caption</th><th>type</th></tr>'
                    + trows + "</table></div>")
    rb = f.get("rename_back") or []
    if rb:
        rrows = "".join(f'<tr><td><code>{esc(r["from"])}</code></td>'
                        f'<td>{esc(r["to"])}</td></tr>' for r in rb)
        rows.append('<div class="kv"><span>Rename-back</span><table class="mini">'
                    '<tr><th>internal</th><th>original</th></tr>' + rrows + "</table></div>")
    inc = f.get("incremental")
    if inc:
        rows.append(f'<div class="kv"><span>Incremental</span>'
                    f'<div>input=<code>{esc(inc["input"])}</code> '
                    f'control=<code>{esc(inc["control_field"])}</code> '
                    f'(append 出力 — run は --incremental)</div></div>')
    chips = [f'<span class="chip">#{s}</span>' for s in f.get("included_steps") or []]
    chips += [f'<span class="chip chip-sp">#{sp["step"]} split {sp["action_indices"]}</span>'
              for sp in f.get("splits") or []]
    if chips:
        rows.append('<div class="kv"><span>元 steps</span><div class="steps">'
                    + " ".join(chips) + "</div></div>")
    desc = esc(f.get("description", ""))
    return (f'<div class="card c-{layer}">'
            f'<div class="card-h"><span class="badge b-{layer}">{esc(LAYER_LABEL[layer])}</span>'
            f'<h3>{esc(f["name"])}</h3></div>'
            f'<div class="card-b">{"".join(rows)}<p class="desc">{desc}</p></div></div>')


# ---------------------------------------------------------------------------
# page assembly
# ---------------------------------------------------------------------------

def render_html(plan: dict, resolver: StepResolver, notes: list[str]) -> str:
    flows = plan["flows"]
    by_layer = {l: [f for f in flows if f["layer"] == l] for l in LAYERS}
    steps = source_steps(resolver)
    assign = compute_assignment(plan, steps)

    summ = "".join(
        f'<span class="scount b-{l}">{LAYER_LABEL[l]} <b>{len(by_layer[l])}</b></span>'
        for l in LAYERS if by_layer[l])

    nodes, edges = build_after_graph(plan, resolver)
    note_html = "".join(f'<li>{esc(n)}</li>' for n in notes)
    notes_block = (f'<details class="alt"><summary>検証ノート ({len(notes)})</summary>'
                   f'<ul class="notes">{note_html}</ul></details>') if notes else ""

    card_html = []
    for l in LAYERS:
        if by_layer[l]:
            card_html.append(f'<h2 class="lh lh-{l}">{LAYER_LABEL[l]} レイヤ '
                             f'<span class="lh-n">{len(by_layer[l])}</span></h2>')
            card_html.append('<div class="cards">'
                             + "".join(card(f, plan, resolver) for f in by_layer[l])
                             + "</div>")

    mapping = [(f.get("source_original_output_name"), f) for f in flows
               if f.get("source_original_output_name")]
    map_rows = "".join(
        f'<tr><td><code>{esc(o)}</code></td><td><code>{esc(f["name"])}</code></td>'
        f'<td><code>{esc((f.get("output") or {}).get("name") or f["name"])}</code></td></tr>'
        for o, f in mapping) or ('<tr><td colspan="3" class="muted">元 output を引き継ぐ '
                                 'flow なし — comparator ペアなし</td></tr>')

    lay_rows = []
    for grp, suf in (("flow_projects", ".tfl"), ("ds_projects", " (PDS)")):
        for l in LAYERS:
            proj = plan[grp][l]
            names = ", ".join(
                x["name"] + (suf if suf == " (PDS)" else ".tfl")
                for x in by_layer[l]
                if not (grp == "flow_projects" and x["kind"] == "pds_augment"))
            lay_rows.append(f'<tr><td><code>{esc(proj["path"])}</code></td>'
                            f'<td>{esc(names) or "—"}</td></tr>')

    alts = "".join(
        f'<details class="alt"><summary>{esc(a.get("title", "案"))}</summary>'
        f'<p>{esc(a.get("body", ""))}</p></details>'
        for a in plan.get("alternatives") or [])
    alts_block = f"<h2>Alternatives considered</h2>{alts}" if alts else ""

    return TEMPLATE.format(
        flow_name=esc(plan["flow_name"]),
        total=esc(plan["source"]["total_nodes"]),
        nflows=len(flows),
        summ=summ,
        notes=notes_block,
        counts=counts_line(assign),
        strip=strip_html(steps, assign),
        asis_dag=svg_asis_full(plan, steps, assign),
        dag=svg_after(nodes, edges),
        cards="".join(card_html),
        map_rows=map_rows,
        lay_rows="".join(lay_rows),
        alts=alts_block,
    )


TEMPLATE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Decomposition Plan · {flow_name}</title>
<style>
:root {{
  --bg:#f7f8fa; --panel:#fff; --ink:#1a1f26; --sub:#5b6672; --line:#e3e7ec;
  --stg:#2f7ed8; --int:#e0982c; --marts:#2fa46b; --src:#8a94a0; --del:#c4554d;
  --stg-w:#e7f1fc; --int-w:#fdf3e2; --marts-w:#e6f6ee; --src-w:#eef0f3; --del-w:#faeae9;
  --code:#f0f2f5;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg:#12151a; --panel:#1a1f27; --ink:#e6eaef; --sub:#9aa5b1; --line:#2a313b;
    --stg:#5ba3ef; --int:#eab861; --marts:#5cc492; --src:#7d8794; --del:#e07a72;
    --stg-w:#16283d; --int-w:#332813; --marts-w:#14301f; --src-w:#232a33; --del-w:#3a1f1d;
    --code:#232a33;
  }}
}}
:root[data-theme=light] {{ --bg:#f7f8fa; --panel:#fff; --ink:#1a1f26; --sub:#5b6672; --line:#e3e7ec; --stg:#2f7ed8; --int:#e0982c; --marts:#2fa46b; --src:#8a94a0; --del:#c4554d; --stg-w:#e7f1fc; --int-w:#fdf3e2; --marts-w:#e6f6ee; --src-w:#eef0f3; --del-w:#faeae9; --code:#f0f2f5; }}
:root[data-theme=dark] {{ --bg:#12151a; --panel:#1a1f27; --ink:#e6eaef; --sub:#9aa5b1; --line:#2a313b; --stg:#5ba3ef; --int:#eab861; --marts:#5cc492; --src:#7d8794; --del:#e07a72; --stg-w:#16283d; --int-w:#332813; --marts-w:#14301f; --src-w:#232a33; --del-w:#3a1f1d; --code:#232a33; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","Yu Gothic UI",Roboto,sans-serif; }}
.wrap {{ max-width:1180px; margin:0 auto; padding:28px 20px 80px; }}
h1 {{ font-size:22px; margin:0 0 4px; }}
h2 {{ font-size:16px; margin:34px 0 12px; padding-bottom:6px; border-bottom:1px solid var(--line); }}
.meta {{ color:var(--sub); font-size:13px; margin-bottom:16px; }}
.summ {{ display:flex; gap:10px; flex-wrap:wrap; margin:14px 0 10px; }}
.scount {{ padding:5px 12px; border-radius:20px; font-size:13px; font-weight:600; }}
.scount b {{ font-size:15px; }}
.b-staging {{ background:var(--stg-w); color:var(--stg); }}
.b-intermediate {{ background:var(--int-w); color:var(--int); }}
.b-marts {{ background:var(--marts-w); color:var(--marts); }}
.b-rep {{ background:var(--src-w); color:var(--sub); }}
.b-del {{ background:var(--del-w); color:var(--del); }}
.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:12px;
  padding:16px; overflow-x:auto; }}
.guide {{ background:var(--panel); border:1px solid var(--line); border-radius:12px;
  padding:12px 16px; font-size:13px; display:grid; gap:4px; }}
.guide-warn {{ color:var(--sub); }}
.del-ink {{ color:var(--del); }}
.strip-hdr {{ font-size:12px; color:var(--sub); margin-bottom:8px; }}
.strip {{ display:flex; flex-wrap:wrap; gap:3px; }}
.cell {{ width:27px; height:23px; border-radius:4px; font-size:10.5px; font-weight:600;
  display:flex; align-items:center; justify-content:center; cursor:default; }}
.cell-pass, .cell-rep {{ background:var(--src-w); color:var(--sub); border:1px dashed var(--src); }}
.cell-del {{ background:var(--del-w); color:var(--del); border:1.5px solid var(--del); }}
.dag {{ width:100%; height:auto; min-width:760px; }}
.dag-asis {{ min-width:0; width:auto; max-width:none; display:block; }}
.dag .colhdr {{ fill:var(--sub); font-size:12px; font-weight:700; letter-spacing:.04em;
  text-transform:uppercase; }}
.dag .edge {{ fill:none; stroke:var(--sub); stroke-width:1.6; opacity:.55; }}
.dag .edge.dashed {{ stroke-dasharray:5 4; opacity:.8; }}
.dag .node rect {{ stroke-width:1.5; }}
.dag .nlabel {{ font-size:11px; font-weight:600; }}
.dag .nsub {{ font-size:9px; opacity:.8; }}
.dag .n-staging rect {{ fill:var(--stg-w); stroke:var(--stg); }}
.dag .n-intermediate rect {{ fill:var(--int-w); stroke:var(--int); }}
.dag .n-marts rect {{ fill:var(--marts-w); stroke:var(--marts); }}
.dag .n-source rect {{ fill:var(--src-w); stroke:var(--src); stroke-dasharray:4 3; }}
.dag .n-rep rect {{ fill:var(--src-w); stroke:var(--src); stroke-dasharray:4 3; }}
.dag .n-del rect {{ fill:var(--del-w); stroke:var(--del); }}
.dag .n-split rect {{ fill:var(--stg-w); stroke:var(--int); stroke-dasharray:6 3; }}
.dag .n-staging text {{ fill:var(--stg); }}
.dag .n-intermediate text {{ fill:var(--int); }}
.dag .n-marts text {{ fill:var(--marts); }}
.dag .n-source text, .dag .n-rep text {{ fill:var(--src); }}
.dag .n-del text {{ fill:var(--del); }}
.dag .n-split text {{ fill:var(--int); }}
.lh {{ font-size:15px; margin:28px 0 10px; }}
.lh-n {{ font-size:12px; color:var(--sub); font-weight:400; }}
.lh-staging {{ color:var(--stg); }} .lh-intermediate {{ color:var(--int); }} .lh-marts {{ color:var(--marts); }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(330px,1fr)); gap:14px; }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:12px;
  overflow:hidden; border-left-width:4px; }}
.card.c-staging {{ border-left-color:var(--stg); }}
.card.c-intermediate {{ border-left-color:var(--int); }}
.card.c-marts {{ border-left-color:var(--marts); }}
.card-h {{ display:flex; align-items:center; gap:8px; padding:12px 14px; border-bottom:1px solid var(--line); }}
.card-h h3 {{ margin:0; font-size:14px; font-family:ui-monospace,Menlo,Consolas,monospace; word-break:break-all; }}
.badge {{ font-size:10px; font-weight:700; padding:2px 8px; border-radius:6px; text-transform:uppercase; }}
.b-staging.badge {{ background:var(--stg); color:#fff; }}
.b-intermediate.badge {{ background:var(--int); color:#fff; }}
.b-marts.badge {{ background:var(--marts); color:#fff; }}
.card-b {{ padding:12px 14px; font-size:13px; }}
.kv {{ display:flex; gap:10px; margin-bottom:8px; }}
.kv > span:first-child {{ flex:0 0 92px; color:var(--sub); font-size:12px; padding-top:1px; }}
.kv .stack > div {{ margin-bottom:3px; }}
code {{ background:var(--code); padding:1px 5px; border-radius:4px;
  font-family:ui-monospace,Menlo,Consolas,monospace; font-size:12px; }}
.tag {{ font-size:10px; padding:1px 6px; border-radius:5px; background:var(--src-w); color:var(--sub); }}
.tag.pass {{ background:var(--int-w); color:var(--int); }}
.tag.orig {{ background:var(--marts-w); color:var(--marts); }}
.desc {{ color:var(--sub); font-size:12.5px; margin:10px 0 0; padding-top:8px; border-top:1px dashed var(--line); }}
.steps .chip {{ display:inline-block; font-size:11px; background:var(--code); color:var(--sub);
  padding:1px 5px; border-radius:4px; margin:0 3px 3px 0; }}
.steps .chip-sp {{ border:1px dashed var(--sub); }}
table.mini {{ border-collapse:collapse; font-size:11.5px; }}
table.mini th, table.mini td {{ border:1px solid var(--line); padding:2px 6px; text-align:left; }}
table.mini th {{ color:var(--sub); font-weight:600; }}
table.big {{ border-collapse:collapse; width:100%; font-size:13px; }}
table.big th, table.big td {{ border:1px solid var(--line); padding:7px 10px; text-align:left; }}
table.big th {{ background:var(--code); color:var(--sub); }}
.muted {{ color:var(--sub); }}
details.alt {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:8px 12px; margin-bottom:8px; }}
details.alt summary {{ cursor:pointer; font-weight:600; }}
details.alt p, details.alt ul {{ color:var(--sub); margin:8px 0 0; }}
.notes {{ padding-left:18px; font-size:12.5px; }}
.legend {{ display:flex; gap:16px; flex-wrap:wrap; margin-top:10px; font-size:12px; color:var(--sub); }}
.legend i {{ display:inline-block; width:12px; height:12px; border-radius:3px; margin-right:5px; vertical-align:-1px; }}
</style></head>
<body><div class="wrap">
<h1>Decomposition Plan · {flow_name}</h1>
<div class="meta">元フロー {total} ノード → 新規成果物 {nflows} 個 &nbsp;·&nbsp;
<b>Stop 2 レビュービュー</b>（plan.json からレンダリング — 修正は JSON へ、このファイルは編集しない）</div>
<div class="summ">{summ}</div>
{notes}

<h2>As-is フロー → 分解先マップ</h2>
<div class="guide">
  <div><b>stg</b> — ソース別の入口整形のみ（転写 / Live PDS 化、型・命名・重複除去）</div>
  <div><b>int</b> — entity 単位にビジネスロジックを集約（Join・フィルタ・計算）</div>
  <div><b>marts</b> — BI が消費する出力粒度（fct / dim / rpt）。元 Output のスキーマを引き継ぐ</div>
  <div class="guide-warn">灰 = 既存 PDS 参照 (passthrough) / 置換される元 Output ・ <b class="del-ink">赤 = どの新フローにも未割当（削除候補 — Stop 2 で要確認）</b></div>
</div>
<div class="summ">{counts}</div>
<div class="panel">
  <div class="strip-hdr">元フローの全ステップ（色 = 行き先レイヤ。flow 単位の対応は下の As-is DAG と各カードの「元 steps」）</div>
  {strip}
</div>
<div class="panel" style="margin-top:12px">
  <div class="strip-hdr">As-is 全ノード DAG（元フローの構造そのまま。各ノード 2 行目 = 行き先）</div>
  {asis_dag}
</div>
<div class="legend">
  <span><i style="background:var(--stg)"></i>staging へ</span>
  <span><i style="background:var(--int)"></i>intermediate へ</span>
  <span><i style="background:var(--marts)"></i>marts へ</span>
  <span><i style="background:var(--src);opacity:.5"></i>passthrough / 置換</span>
  <span><i style="background:var(--del)"></i>未割当 (削除候補)</span>
</div>

<h2>依存 DAG（分解後）</h2>
<div class="panel">{dag}</div>
<div class="legend">
  <span><i style="background:var(--stg)"></i>staging</span>
  <span><i style="background:var(--int)"></i>intermediate</span>
  <span><i style="background:var(--marts)"></i>marts</span>
  <span><i style="background:var(--src);border:1px dashed var(--src)"></i>外部ソース / 置換される元 Output（mart からの破線 = comparator ペア）</span>
</div>

<h2>New .tfl files</h2>
{cards}

<h2>Output mapping（元 → 分解後）</h2>
<table class="big"><tr><th>Original output PDS</th><th>Decomposed flow</th><th>Decomposed output PDS</th></tr>{map_rows}</table>

<h2>Target Tableau Cloud project layout</h2>
<table class="big"><tr><th>Subproject</th><th>Contains</th></tr>{lay_rows}</table>

{alts}
</div>
</body></html>"""
