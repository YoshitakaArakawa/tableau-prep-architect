#!/usr/bin/env python3
"""Dump all actions inside SuperTransform nodes from a flow.json.

Each SuperTransform's beforeActionAnnotations is iterated and summarised by type:
- RenameColumn: old -> new
- ChangeColumnType: column -> new type
- AddColumn: column = expression (one-liner)
- RemoveColumns: list of dropped columns
- ValueFilter / FilterOperation: raw JSON summary
- (other) raw type

Usage:
    python inspect_actions.py path/to/flow.json
    python inspect_actions.py path/to/flow.json -o output.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def summarise_action(i: int, an: dict) -> str:
    t = an.get('nodeType', '').split('.')[-1]
    if t == 'RenameColumn':
        src = an.get('columnName', '?')
        tgt = an.get('rename', '?')
        return f"{i}. **Rename**: `{src}` → `{tgt}`"
    if t == 'ChangeColumnType':
        # Two shapes seen: a flat {columnName, newType} and a
        # {fields: {<col>: {type: <t>, calc: ...}}} map (one entry per cast).
        fields = an.get('fields')
        if isinstance(fields, dict) and fields:
            casts = ', '.join(
                f"`{col}` → `{(spec or {}).get('type', '?')}`"
                for col, spec in fields.items()
            )
            return f"{i}. **ChangeColumnType**: {casts}"
        col = an.get('columnName', '?')
        nt = an.get('newType') or an.get('typeRef') or an.get('newColumnType') or '?'
        return f"{i}. **ChangeColumnType**: `{col}` → `{nt}`"
    if t == 'AddColumn':
        col = an.get('columnName', '?')
        expr = an.get('expression', '?')
        expr_one = ' '.join(expr.split()) if isinstance(expr, str) else str(expr)
        if len(expr_one) > 200:
            expr_one = expr_one[:197] + '...'
        return f"{i}. **AddColumn**: `{col}` = `{expr_one}`"
    if t == 'RemoveColumns':
        cols = an.get('columnNames') or an.get('columns') or []
        if isinstance(cols, list):
            cols_s = ', '.join(f'`{c}`' for c in cols)
        else:
            cols_s = str(cols)
        return f"{i}. **RemoveColumns**: {cols_s}"
    if t in ('ValueFilter', 'FilterOperation'):
        col = an.get('columnName') or an.get('column') or '?'
        expr = an.get('expression') or an.get('filterExpression') or ''
        if isinstance(expr, str):
            expr_one = ' '.join(expr.split())[:150]
        else:
            expr_one = json.dumps(expr, ensure_ascii=False)[:150]
        return f"{i}. **{t}**: column=`{col}` expr=`{expr_one}`"
    raw = json.dumps(an, ensure_ascii=False)
    if len(raw) > 200:
        raw = raw[:197] + '...'
    return f"{i}. **{t}**: {raw}"


def build_report(flow_path: Path) -> str:
    flow = json.loads(flow_path.read_text(encoding='utf-8'))
    nodes = flow['nodes']

    initial = flow.get('initialNodes', [])
    visited: list[str] = []
    queue = list(initial)
    while queue:
        cur = queue.pop(0)
        if cur in visited:
            continue
        visited.append(cur)
        for nxt in nodes[cur].get('nextNodes', []):
            nid_next = nxt.get('nextNodeId') if isinstance(nxt, dict) else nxt
            if nid_next and nid_next not in visited and nid_next not in queue:
                queue.append(nid_next)
    for nid in nodes:
        if nid not in visited:
            visited.append(nid)
    sid = {nid: i + 1 for i, nid in enumerate(visited)}

    lines: list[str] = []
    lines.append(f"# Actions inventory: {flow_path.name}")
    lines.append('')
    lines.append(f"Source: `{flow_path}`")
    lines.append('')

    for nid in visited:
        n = nodes[nid]
        if not n.get('nodeType', '').endswith('SuperTransform'):
            continue
        actions = n.get('beforeActionAnnotations', []) or []
        name = n.get('name', '?')
        lines.append(f"## #{sid[nid]}: {name}  ({len(actions)} actions)")
        lines.append('')
        if not actions:
            lines.append('_(no actions — empty Clean step)_')
            lines.append('')
            continue
        for i, annot in enumerate(actions, 1):
            an = annot.get('annotationNode', {})
            lines.append(summarise_action(i, an))
        lines.append('')

    return '\n'.join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    p.add_argument('flow_path', type=Path, help='Path to flow.json (extracted from .tfl/.tflx)')
    p.add_argument('-o', '--output', type=Path, help='Write to UTF-8 markdown file instead of stdout')
    args = p.parse_args()

    text = build_report(args.flow_path)

    if args.output:
        args.output.write_text(text, encoding='utf-8')
        print(f'Wrote {len(text):,} chars to: {args.output}', file=sys.stderr)
    else:
        try:
            sys.stdout.reconfigure(encoding='utf-8')
        except Exception:
            pass
        print(text)


if __name__ == '__main__':
    main()
