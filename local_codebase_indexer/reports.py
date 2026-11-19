"""Offline, escaped SQL impact reports with source evidence and a bounded SVG graph."""

from __future__ import annotations

import hashlib
import html
import json
import textwrap
from collections import defaultdict, deque
from datetime import datetime, timezone

from . import __version__
from .rendering import render_data_access


def graph(data: dict) -> tuple[list[dict], list[tuple[int, int]], bool]:
    nodes: list[dict] = []
    indexes: dict[tuple, int] = {}
    edges: set[tuple[int, int]] = set()
    truncated = False

    def add(step: dict) -> int:
        key = (
            step.get("symbol"),
            step.get("file_path"),
            step.get("line"),
            step.get("confidence"),
        )
        if key not in indexes:
            indexes[key] = len(nodes)
            nodes.append(step)
        return indexes[key]

    paths = [
        entry.get("path", [])
        for section in (
            "readers",
            "writers",
            "executed_by",
            "possible",
            "schema_changes",
        )
        for entry in data.get(section, [])
    ]
    for program in data.get("programs", []):
        for trace in program.get("paths", []):
            paths.append(trace["steps"])
            for page in trace.get("pages", []):
                paths.append(
                    [{"symbol": page["file_path"], **page, "relation": "page action"}]
                    + trace["steps"]
                )
    for path in paths:
        if len(nodes) + len(path) > 250:
            truncated = True
            continue
        ids = [add(step) for step in path]
        edges.update((a, b) for a, b in zip(ids, ids[1:]) if a != b)
    return nodes, sorted(edges), truncated


def mermaid(data: dict) -> str:
    nodes, edges, clipped = graph(data)
    out = ["flowchart LR"]
    for i, step in enumerate(nodes):
        label = str(step.get("symbol", "?"))
        if step.get("file_path"):
            label += f" | {step['file_path']}:{step.get('line', '?')}"
        label += " | " + step.get("confidence", "static object")
        # Mermaid source is text, but also safe when pasted into an HTML renderer.
        label = html.escape(label, quote=True).replace("\n", " ").replace("\r", " ")
        out.append(f'  n{i}["{label}"]')
    out.extend(f"  n{a} --> n{b}" for a, b in edges)
    if clipped:
        out.append(
            '  warning["Graph limited to 250 nodes; consult full evidence below"]'
        )
    return "\n".join(out) + "\n"


def svg(data: dict) -> str:
    nodes, edges, clipped = graph(data)
    incoming = {i: 0 for i in range(len(nodes))}
    children: dict[int, list[int]] = defaultdict(list)
    for a, b in edges:
        incoming[b] += 1
        children[a].append(b)
    ready = deque(i for i in incoming if not incoming[i])
    levels = {i: 0 for i in incoming}
    visited = set()
    while ready:
        a = ready.popleft()
        visited.add(a)
        for b in children[a]:
            levels[b] = max(levels[b], levels[a] + 1)
            incoming[b] -= 1
            if not incoming[b]:
                ready.append(b)
    # Cycle fallback is bounded and never recurses through user code.
    for i in incoming:
        if i not in visited:
            levels[i] = max(levels.values(), default=0) + 1
    groups: dict[int, list[int]] = defaultdict(list)
    for i, level in levels.items():
        groups[level].append(i)
    columns = max((len(group) for group in groups.values()), default=1)
    width = columns * 400 + 10
    height = (max(levels.values(), default=0) + 1) * 110 + 30
    positions = {}
    for level, group in groups.items():
        for slot, i in enumerate(group):
            positions[i] = (
                (columns - len(group)) * 200 + slot * 400 + 5,
                level * 110 + 5,
            )
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Static source evidence graph" viewBox="0 0 {width} {height}">'
    ]
    out.append(
        '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 Z" fill="#64748b"/></marker></defs>'
    )
    for a, b in edges:
        ax, ay = positions[a][0] + 190, positions[a][1] + 39
        bx, by = positions[b][0] + 190, positions[b][1] + 39
        dx, dy = bx - ax, by - ay
        factor = min(
            190 / abs(dx) if dx else float("inf"), 39 / abs(dy) if dy else float("inf")
        )
        if levels[b] - levels[a] != 1:
            # Long edges travel in row gaps and an outside lane, not behind
            # unrelated method boxes where they could imply a false dependency.
            lane = width - 6
            route = f"M {ax} {ay + 39} L {ax} {ay + 49} L {lane} {ay + 49} L {lane} {by - 49} L {bx} {by - 49} L {bx} {by - 39}"
        else:
            route = f"M {ax + factor * dx} {ay + factor * dy} L {bx - factor * dx} {by - factor * dy}"
        out.append(
            f'<path d="{route}" stroke="#64748b" fill="none" marker-end="url(#arrow)"/>'
        )
    for i, step in enumerate(nodes):
        x, y = positions[i]
        color = {
            "confirmed": "#dcfce7",
            "probable": "#fef3c7",
            "uncertain": "#e2e8f0",
        }.get(step.get("confidence", ""), "#dbeafe")
        label = html.escape(str(step.get("symbol", "?")))
        label_lines = textwrap.wrap(str(step.get("symbol", "?")), width=48)[:2] or ["?"]
        source = html.escape(f"{step.get('file_path', '')}:{step.get('line', '')}"[:95])
        confidence = html.escape(step.get("confidence", "static object"))
        out.append(
            f'<g><title>{label} {source} {confidence}</title><rect x="{x}" y="{y}" width="380" height="78" rx="8" fill="{color}" stroke="#94a3b8"/>'
        )
        out.extend(
            f'<text x="{x + 8}" y="{y + 18 + 15 * j}" font-size="12">{html.escape(line)}</text>'
            for j, line in enumerate(label_lines)
        )
        out.append(
            f'<text x="{x + 8}" y="{y + 51}" font-size="10">{source}</text><text x="{x + 8}" y="{y + 69}" font-size="11">{confidence}</text></g>'
        )
    out.append("</svg>")
    if clipped:
        out.append(
            "<p>Graph limited to 250 nodes. Full structured evidence is included below.</p>"
        )
    return "".join(out)


def render_report(data: dict, status: dict, format: str = "html") -> str:
    if format == "mermaid":
        return mermaid(data)
    report = {
        "schema_version": 1,
        "package_version": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "index": status,
        "data_access": data,
    }
    if format == "json":
        return json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    title = html.escape("SQL impact: " + data["object"])
    summary = html.escape(render_data_access(data, full=True))
    evidence = html.escape(json.dumps(report, ensure_ascii=False, indent=2))
    fingerprint = hashlib.sha256(
        json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    warning = (
        "This query is ambiguous; choose a qualified object."
        if data.get("ambiguous")
        else "Partial analysis: inspect coverage warnings for depth limits or unresolved definitions."
        if not data.get("coverage", {}).get("complete_within_limits", True)
        else "Static analysis within configured traversal limits; unsupported patterns may be missing."
    )
    return f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:">
<title>{title}</title><style>body{{font:16px system-ui;max-width:1200px;margin:2rem auto;padding:0 1rem;color:#0f172a}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f1f5f9;padding:1rem;border-radius:8px}}.graph{{overflow-x:auto}}svg{{width:100%;height:auto;min-width:760px}}.notice{{background:#fef3c7;padding:1rem}}small{{overflow-wrap:anywhere}}</style>
<h1>{title}</h1><p class="notice">{warning} Confidence labels are evidence classifications, not runtime proof.</p>
<p>Package {__version__} · Index revision {html.escape(str(status.get("index_revision", "unknown")))} · Status {html.escape(status.get("state", "unknown"))}</p>
<h2>Evidence graph</h2><div class="graph">{svg(data)}</div><h2>Access report</h2><pre>{summary}</pre>
<details><summary>Mermaid source</summary><pre>{html.escape(mermaid(data))}</pre></details>
<details><summary>Complete structured evidence and index metadata</summary><pre>{evidence}</pre></details>
<small>Evidence SHA256: {fingerprint}. File paths and code excerpts are included; review before sharing.</small></html>"""
