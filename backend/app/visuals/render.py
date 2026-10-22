"""Spec -> SVG, deterministic and dependency-free. Every renderer returns the SVG text plus the boxes it drew, so the
verifier can check that nothing overlaps or falls outside the canvas.

Both colour themes are in the SVG itself: light colours by default, dark ones under @media (prefers-color-scheme: dark),
which matches how the rest of the UI switches theme. All text is XML-escaped (specs come from a model: never trusted).
"""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional
from xml.sax.saxutils import escape

from . import safeexpr
from .specs import (ArraySpec, FlowchartSpec, GraphSpec, LinkedListSpec, PlotSpec, StackQueueSpec, TableSpec, TimelineSpec, TreeSpec)

# Colours come from the page's theme tokens (--vz-*), so a theme switch restyles diagrams instantly. The fallbacks make the SVG
# look right on its own too: light by default, dark under prefers-color-scheme.
LIGHT = {"node-fill": "#eef1fa", "node-stroke": "#4b5b8a", "hl-fill": "#fde9c8", "hl-stroke": "#c4561d", "start-fill": "#e3f2ea",
         "start-stroke": "#2e7d5b", "end-fill": "#f9e3e1", "end-stroke": "#b3261e", "text": "#0e1b2b", "small": "#4a5260", "edge": "#5d6572",
         "grid": "#d9dde6", "line": "#4b5b8a", "head-fill": "#dfe5f5", "bg": "#ffffff"}
DARK = {"node-fill": "#232a3d", "node-stroke": "#8b9bd0", "hl-fill": "#4a3a1f", "hl-stroke": "#e6a050", "start-fill": "#1f3a2c",
        "start-stroke": "#5cc98f", "end-fill": "#40201e", "end-stroke": "#ff8a80", "text": "#f1efe9", "small": "#aab0bb", "edge": "#9aa0aa",
        "grid": "#2e3340", "line": "#8b9bd0", "head-fill": "#2b3350", "bg": "#14161a"}


def _rules(pal: dict) -> str:
    v = lambda k: f"var(--vz-{k},{pal[k]})"  # noqa: E731
    return (f".vz-node{{fill:{v('node-fill')};stroke:{v('node-stroke')};stroke-width:1.5}}.vz-hl{{fill:{v('hl-fill')};stroke:{v('hl-stroke')};stroke-width:1.8}}"
            f".vz-start{{fill:{v('start-fill')};stroke:{v('start-stroke')};stroke-width:1.5}}.vz-end{{fill:{v('end-fill')};stroke:{v('end-stroke')};stroke-width:1.5}}"
            f".vz-text{{fill:{v('text')};font:500 13px system-ui,-apple-system,'Segoe UI',sans-serif}}.vz-small{{fill:{v('small')};font:11.5px system-ui,sans-serif}}"
            f".vz-edge{{stroke:{v('edge')};stroke-width:1.5;fill:none}}.vz-arrow{{fill:{v('edge')}}}.vz-axis{{stroke:{v('edge')};stroke-width:1.2;fill:none}}"
            f".vz-grid{{stroke:{v('grid')};stroke-width:1}}.vz-line{{stroke:{v('line')};stroke-width:2.2;fill:none}}"
            f".vz-head{{fill:{v('head-fill')};stroke:{v('node-stroke')};stroke-width:1.5}}.vz-bg{{fill:{v('bg')}}}.vz-label-bg{{fill:{v('bg')};opacity:.85}}")


STYLE = f"<style>{_rules(LIGHT)}@media (prefers-color-scheme: dark){{{_rules(DARK)}}}</style>"
DEFS = ('<defs><marker id="vz-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M0 0L10 5L0 10z" class="vz-arrow"/></marker></defs>')
CHAR_W, LINE_H = 7.3, 16


@dataclass
class Rendered:
    svg: str
    width: int
    height: int
    boxes: list[tuple[float, float, float, float, str]] = field(default_factory=list)   # x, y, w, h, label
    labels: list[str] = field(default_factory=list)


def esc(s) -> str:
    return escape(str(s), {'"': "&quot;"})


def wrap(text: str, width: int = 14, max_lines: int = 3) -> list[str]:
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][: max(1, width - 1)] + "…"
    return lines or [""]


def text_block(cx: float, cy: float, lines: list[str], cls: str = "vz-text") -> str:
    y0 = cy - (len(lines) - 1) * LINE_H / 2 + 4.5
    return "".join(f'<text x="{cx:.1f}" y="{y0 + i * LINE_H:.1f}" text-anchor="middle" class="{cls}">{esc(l)}</text>' for i, l in enumerate(lines))


def box_w(lines: list[str], minimum: float = 64, pad: float = 24) -> float:
    return max(minimum, max(len(l) for l in lines) * CHAR_W + pad)


def wrap_svg(width: float, height: float, body: str, title: str, alt: str, boxes, labels) -> Rendered:
    w, h = int(math.ceil(width)), int(math.ceil(height))
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" '
           f'aria-label="{esc(alt)}" class="vz"><title>{esc(title)}</title><desc>{esc(alt)}</desc>{STYLE}{DEFS}{body}</svg>')
    return Rendered(svg, w, h, boxes, labels)


# ============================================================================== flowchart
def _flow_layout(spec: FlowchartSpec):
    ids = [n.id for n in spec.nodes]
    out: dict[str, list[str]] = {i: [] for i in ids}
    for e in spec.edges:
        out[e.from_].append(e.to)
    # depth-first search from the first node: edges that point at a node on the current path are loop-backs
    back: set[tuple[str, str]] = set()
    state: dict[str, int] = {}

    def dfs(u):
        state[u] = 1
        for v in out[u]:
            if state.get(v) == 1:
                back.add((u, v))
            elif v not in state:
                dfs(v)
        state[u] = 2

    root = next((n.id for n in spec.nodes if n.shape == "start"), ids[0])
    dfs(root)
    for i in ids:
        if i not in state:
            dfs(i)
    rank = {i: 0 for i in ids}
    for _ in range(len(ids)):  # longest path over forward edges
        changed = False
        for e in spec.edges:
            if (e.from_, e.to) not in back and rank[e.to] < rank[e.from_] + 1:
                rank[e.to] = rank[e.from_] + 1
                changed = True
        if not changed:
            break
    return rank, back


def render_flowchart(spec: FlowchartSpec) -> Rendered:
    rank, back = _flow_layout(spec)
    size, lines_of = {}, {}
    for n in spec.nodes:
        lines = wrap(n.label, 15 if n.shape != "decision" else 13, 2)
        lines_of[n.id] = lines
        w = box_w(lines, 70, 26) + (36 if n.shape == "decision" else 8 if n.shape == "io" else 0)
        h = 64 if n.shape == "decision" else (50 if len(lines) > 1 else 42)
        size[n.id] = (w, h)
    rows: dict[int, list[str]] = {}
    for n in spec.nodes:
        rows.setdefault(rank[n.id], []).append(n.id)
    GAP_X, GAP_Y, MARGIN = 46, 54, 36
    row_w = {r: sum(size[i][0] for i in ids) + GAP_X * (len(ids) - 1) for r, ids in rows.items()}
    lanes = max(1, len([1 for e in spec.edges if (e.from_, e.to) in back]))
    width = max(row_w.values()) + MARGIN * 2 + 24 * lanes
    pos, y = {}, 30.0
    for r in sorted(rows):
        rh = max(size[i][1] for i in rows[r])
        x = MARGIN + (width - 24 * lanes - MARGIN * 2 - row_w[r]) / 2
        for i in rows[r]:
            pos[i] = (x + size[i][0] / 2, y + rh / 2)
            x += size[i][0] + GAP_X
        y += rh + GAP_Y
    height = y - GAP_Y + 30
    body, boxes, labels = [], [], []
    lane = 0
    for e in spec.edges:  # edges first so nodes draw on top
        (sx, sy), (tx, ty) = pos[e.from_], pos[e.to]
        (sw, sh), (tw, th) = size[e.from_], size[e.to]
        if (e.from_, e.to) in back:
            mx = width - 24 * (lanes - lane) + 6
            lane += 1
            body.append(f'<path class="vz-edge" marker-end="url(#vz-arrow)" d="M{sx + sw / 2:.1f} {sy:.1f} L{mx:.1f} {sy:.1f} L{mx:.1f} {ty:.1f} L{tx + tw / 2:.1f} {ty:.1f}"/>')
            lx, ly = mx - 4, (sy + ty) / 2
        else:
            x1, y1, x2, y2 = sx, sy + sh / 2, tx, ty - th / 2
            if abs(sx - tx) > 1 and ty > sy:  # leave from the side of a decision for its branches
                if spec.nodes[[n.id for n in spec.nodes].index(e.from_)].shape == "decision":
                    x1, y1 = (sx - sw / 2, sy) if tx < sx else (sx + sw / 2, sy)
            my = (y1 + y2) / 2
            body.append(f'<path class="vz-edge" marker-end="url(#vz-arrow)" d="M{x1:.1f} {y1:.1f} C{x1:.1f} {my:.1f} {x2:.1f} {my:.1f} {x2:.1f} {y2:.1f}"/>')
            lx, ly = (x1 + x2) / 2 + (10 if tx >= sx else -10), (y1 + y2) / 2 - 2
        if e.label:
            body.append(f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle" class="vz-small">{esc(e.label)}</text>')
            labels.append(e.label)
    for n in spec.nodes:
        cx, cy = pos[n.id]
        w, h = size[n.id]
        x0, y0 = cx - w / 2, cy - h / 2
        cls = {"start": "vz-start", "end": "vz-end"}.get(n.shape, "vz-node")
        if n.shape == "decision":
            body.append(f'<polygon class="{cls}" points="{cx:.1f},{y0:.1f} {x0 + w:.1f},{cy:.1f} {cx:.1f},{y0 + h:.1f} {x0:.1f},{cy:.1f}"/>')
        elif n.shape in ("start", "end"):
            body.append(f'<rect class="{cls}" x="{x0:.1f}" y="{y0:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{h / 2:.1f}"/>')
        elif n.shape == "io":
            sk = 12
            body.append(f'<polygon class="{cls}" points="{x0 + sk:.1f},{y0:.1f} {x0 + w:.1f},{y0:.1f} {x0 + w - sk:.1f},{y0 + h:.1f} {x0:.1f},{y0 + h:.1f}"/>')
        else:
            body.append(f'<rect class="{cls}" x="{x0:.1f}" y="{y0:.1f}" width="{w:.1f}" height="{h:.1f}" rx="6"/>')
        body.append(text_block(cx, cy, lines_of[n.id]))
        body[-2:] = ['<g class="fn" data-n="' + esc(n.id) + '">' + body[-2] + body[-1] + '</g>']       # one group per node: the page links it to the code lines
        boxes.append((x0, y0, w, h, n.label))
        labels.append(n.label)
    return wrap_svg(width, height, "".join(body), spec.title, spec.alt_text, boxes, labels)


# ==================================================================================== array
def render_array(spec: ArraySpec) -> Rendered:
    cell_w, cell_h, top, left = 58.0, 44.0, 52.0, 30.0
    n = len(spec.values)
    width, height = left * 2 + cell_w * n, top + cell_h + 56
    body, boxes, labels = [], [], []
    for i, v in enumerate(spec.values):
        x = left + i * cell_w
        body.append(f'<rect class="{"vz-hl" if i in spec.highlight else "vz-node"}" x="{x:.1f}" y="{top}" width="{cell_w}" height="{cell_h}"/>')
        body.append(text_block(x + cell_w / 2, top + cell_h / 2, wrap(str(v), 7, 2)))
        body.append(f'<text x="{x + cell_w / 2:.1f}" y="{top + cell_h + 18}" text-anchor="middle" class="vz-small">{i}</text>')
        boxes.append((x, top, cell_w, cell_h, str(v)))
        labels.append(str(v))
    for p in spec.pointers:
        cx = left + p.index * cell_w + cell_w / 2
        body.append(f'<path class="vz-edge" marker-end="url(#vz-arrow)" d="M{cx:.1f} {top - 30} L{cx:.1f} {top - 3}"/>')
        body.append(f'<text x="{cx:.1f}" y="{top - 36}" text-anchor="middle" class="vz-text">{esc(p.name)}</text>')
        labels.append(p.name)
    return wrap_svg(width, height, "".join(body), spec.title, spec.alt_text, boxes, labels)


# ============================================================================ linked list
def render_linked_list(spec: LinkedListSpec) -> Rendered:
    per_row, node_w, ptr_w, h, gap, left, top = 6, 76.0, 24.0, 42.0, 34.0, 36.0, 50.0
    n = len(spec.values)
    rows = math.ceil(n / per_row)
    width = left * 2 + min(n, per_row) * (node_w + ptr_w) + (min(n, per_row) - 1) * gap + 50
    height = top + rows * (h + 46) + 10
    body, boxes, labels = [], [], []
    centers = []
    for i, v in enumerate(spec.values):
        r, c = divmod(i, per_row)
        x, y = left + c * (node_w + ptr_w + gap), top + r * (h + 46)
        body.append(f'<rect class="vz-node" x="{x:.1f}" y="{y:.1f}" width="{node_w}" height="{h}"/>')
        body.append(f'<rect class="vz-head" x="{x + node_w:.1f}" y="{y:.1f}" width="{ptr_w}" height="{h}"/>')
        body.append(text_block(x + node_w / 2, y + h / 2, wrap(str(v), 9, 2)))
        boxes.append((x, y, node_w + ptr_w, h, str(v)))
        labels.append(str(v))
        centers.append((x, y))
    for i in range(n):
        x, y = centers[i]
        if i < n - 1:
            nx, ny = centers[i + 1]
            if ny == y:
                body.append(f'<path class="vz-edge" marker-end="url(#vz-arrow)" d="M{x + node_w + ptr_w / 2:.1f} {y + h / 2:.1f} L{nx - 2:.1f} {y + h / 2:.1f}"/>')
                if spec.doubly:
                    body.append(f'<path class="vz-edge" marker-end="url(#vz-arrow)" d="M{nx + 6:.1f} {y + h - 8:.1f} L{x + node_w + ptr_w + 2:.1f} {y + h - 8:.1f}"/>')
            else:  # wrap to the next row
                sx, sy = x + node_w + ptr_w / 2, y + h
                body.append(f'<path class="vz-edge" marker-end="url(#vz-arrow)" d="M{sx:.1f} {sy:.1f} L{sx:.1f} {sy + 22:.1f} L{nx + node_w / 2:.1f} {sy + 22:.1f} L{nx + node_w / 2:.1f} {ny - 2:.1f}"/>')
        else:
            body.append(f'<path class="vz-edge" d="M{x + node_w + ptr_w / 2:.1f} {y + h / 2:.1f} L{x + node_w + ptr_w + 14:.1f} {y + h / 2:.1f}"/>')
            body.append(f'<text x="{x + node_w + ptr_w + 18:.1f}" y="{y + h / 2 + 4:.1f}" class="vz-small">null</text>')
    if spec.head_label:
        x, y = centers[0]
        body.append(f'<text x="{x + node_w / 2:.1f}" y="{y - 22}" text-anchor="middle" class="vz-text">{esc(spec.head_label)}</text>')
        body.append(f'<path class="vz-edge" marker-end="url(#vz-arrow)" d="M{x + node_w / 2:.1f} {y - 16} L{x + node_w / 2:.1f} {y - 3}"/>')
        labels.append(spec.head_label)
    return wrap_svg(width, height, "".join(body), spec.title, spec.alt_text, boxes, labels)


# ============================================================================ stack / queue
def render_stack_queue(spec: StackQueueSpec) -> Rendered:
    n, body, boxes, labels = len(spec.items), [], [], []
    if spec.kind == "stack":
        w, h, left, top = 130.0, 38.0, 70.0, 30.0
        width, height = left * 2 + w + 40, top + n * h + 70
        for i, v in enumerate(reversed(spec.items)):  # last pushed on top
            y = top + i * h
            body.append(f'<rect class="{"vz-hl" if i == 0 else "vz-node"}" x="{left}" y="{y:.1f}" width="{w}" height="{h}"/>')
            body.append(text_block(left + w / 2, y + h / 2, wrap(str(v), 16, 1)))
            boxes.append((left, y, w, h, str(v)))
            labels.append(str(v))
        body.append(f'<text x="{left + w + 14}" y="{top + h / 2 + 4}" class="vz-text">top</text>')
        body.append(f'<path class="vz-edge" marker-end="url(#vz-arrow)" d="M{left + w + 40} {top + h / 2} L{left + w + 3} {top + h / 2}"/>')
        op_y = top + n * h + 28
    else:
        w, h, left, top = 66.0, 44.0, 36.0, 44.0
        width, height = left * 2 + n * w, top + h + 64
        for i, v in enumerate(spec.items):
            x = left + i * w
            body.append(f'<rect class="{"vz-hl" if i == 0 else "vz-node"}" x="{x:.1f}" y="{top}" width="{w}" height="{h}"/>')
            body.append(text_block(x + w / 2, top + h / 2, wrap(str(v), 8, 2)))
            boxes.append((x, top, w, h, str(v)))
            labels.append(str(v))
        body.append(f'<text x="{left + w / 2:.1f}" y="{top - 12}" text-anchor="middle" class="vz-text">front</text>')
        body.append(f'<text x="{left + (n - 0.5) * w:.1f}" y="{top - 12}" text-anchor="middle" class="vz-text">back</text>')
        op_y = top + h + 34
    if spec.operation:
        body.append(f'<text x="{width / 2:.1f}" y="{op_y:.1f}" text-anchor="middle" class="vz-small">{esc(spec.operation)}</text>')
        labels.append(spec.operation)
    return wrap_svg(width, height, "".join(body), spec.title, spec.alt_text, boxes, labels)


# ================================================================================== tree
def render_tree(spec: TreeSpec) -> Rendered:
    kids: dict[str, list[str]] = {n.id: [] for n in spec.nodes}
    has_parent = set()
    for e in spec.edges:
        kids[e.parent].append(e.child)
        has_parent.add(e.child)
    root = next(n.id for n in spec.nodes if n.id not in has_parent)
    lab = {n.id: n.label for n in spec.nodes}
    lines = {i: wrap(lab[i], 11, 2) for i in lab}
    nw = {i: box_w(lines[i], 52, 20) for i in lab}
    slot, nh, GY, left, top = max(nw.values()) + 18, 40.0, 62.0, 30.0, 28.0
    xs, depth, counter = {}, {}, [0]

    def place(u, d):
        depth[u] = d
        if not kids[u]:
            xs[u] = counter[0] * slot
            counter[0] += 1
        else:
            for c in kids[u]:
                place(c, d + 1)
            xs[u] = sum(xs[c] for c in kids[u]) / len(kids[u])

    place(root, 0)
    width = left * 2 + max(counter[0] - 1, 0) * slot + max(nw.values()) + 10
    height = top * 2 + (max(depth.values())) * (nh + GY) + nh
    body, boxes, labels = [], [], []
    cx = {i: left + xs[i] + max(nw.values()) / 2 for i in lab}
    cy = {i: top + depth[i] * (nh + GY) + nh / 2 for i in lab}
    for e in spec.edges:
        body.append(f'<path class="vz-edge" d="M{cx[e.parent]:.1f} {cy[e.parent] + nh / 2:.1f} L{cx[e.child]:.1f} {cy[e.child] - nh / 2:.1f}"/>')
    for n in spec.nodes:
        w = nw[n.id]
        cls = "vz-hl" if n.id in spec.highlight else "vz-node"
        body.append(f'<rect class="{cls}" x="{cx[n.id] - w / 2:.1f}" y="{cy[n.id] - nh / 2:.1f}" width="{w:.1f}" height="{nh}" rx="{nh / 2}"/>')
        body.append(text_block(cx[n.id], cy[n.id], lines[n.id]))
        boxes.append((cx[n.id] - w / 2, cy[n.id] - nh / 2, w, nh, n.label))
        labels.append(n.label)
    return wrap_svg(width, height, "".join(body), spec.title, spec.alt_text, boxes, labels)


# ================================================================================= graph
def render_graph(spec: GraphSpec) -> Rendered:
    n = len(spec.nodes)
    lines = {x.id: wrap(x.label, 10, 2) for x in spec.nodes}
    nw = {i: box_w(l, 54, 18) for i, l in lines.items()}
    R = max(100.0, n * 24.0)
    cx0 = R + max(nw.values()) / 2 + 30
    cy0 = R + 56
    width, height = cx0 * 2, cy0 + R + 56
    pos = {x.id: (cx0 + R * math.cos(-math.pi / 2 + 2 * math.pi * i / n), cy0 + R * math.sin(-math.pi / 2 + 2 * math.pi * i / n)) for i, x in enumerate(spec.nodes)}
    nh = 38.0
    body, boxes, labels = [], [], []

    def clip(p, q, w):  # point on the border of node p's box towards q
        dx, dy = q[0] - p[0], q[1] - p[1]
        if dx == 0 and dy == 0:
            return p
        t = min((w / 2) / abs(dx) if dx else 1e9, (nh / 2) / abs(dy) if dy else 1e9)
        return (p[0] + dx * t, p[1] + dy * t)

    for e in spec.edges:
        p, q = pos[e.from_], pos[e.to]
        a, b = clip(p, q, nw[e.from_]), clip(q, p, nw[e.to])
        marker = ' marker-end="url(#vz-arrow)"' if spec.directed else ""
        body.append(f'<path class="vz-edge"{marker} d="M{a[0]:.1f} {a[1]:.1f} L{b[0]:.1f} {b[1]:.1f}"/>')
        if e.weight is not None:
            mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            body.append(f'<rect class="vz-label-bg" x="{mx - 12:.1f}" y="{my - 9:.1f}" width="24" height="16" rx="3"/>'
                        f'<text x="{mx:.1f}" y="{my + 4:.1f}" text-anchor="middle" class="vz-small">{esc(e.weight)}</text>')
    for x in spec.nodes:
        px, py = pos[x.id]
        w = nw[x.id]
        body.append(f'<rect class="vz-node" x="{px - w / 2:.1f}" y="{py - nh / 2:.1f}" width="{w:.1f}" height="{nh}" rx="{nh / 2}"/>')
        body.append(text_block(px, py, lines[x.id]))
        boxes.append((px - w / 2, py - nh / 2, w, nh, x.label))
        labels.append(x.label)
    return wrap_svg(width, height, "".join(body), spec.title, spec.alt_text, boxes, labels)


# ================================================================================= table
def render_table(spec: TableSpec) -> Rendered:
    cols = len(spec.columns)
    widths = [max(len(str(spec.columns[c])), *(len(str(r[c])) for r in spec.rows)) * CHAR_W + 26 for c in range(cols)]
    widths = [max(60.0, w) for w in widths]
    rh, left, top = 34.0, 20.0, 20.0
    width, height = left * 2 + sum(widths), top * 2 + rh * (len(spec.rows) + 1)
    body, boxes, labels = [], [], []
    y = top
    for ri, row in enumerate([spec.columns] + spec.rows):
        x = left
        for ci, cell in enumerate(row):
            cls = "vz-head" if ri == 0 else "vz-node"
            body.append(f'<rect class="{cls}" x="{x:.1f}" y="{y:.1f}" width="{widths[ci]:.1f}" height="{rh}"/>')
            body.append(text_block(x + widths[ci] / 2, y + rh / 2, [str(cell)[:26]], "vz-text"))
            boxes.append((x, y, widths[ci], rh, str(cell)))
            labels.append(str(cell))
            x += widths[ci]
        y += rh
    return wrap_svg(width, height, "".join(body), spec.title, spec.alt_text, boxes, labels)


# =============================================================================== timeline
def render_timeline(spec: TimelineSpec) -> Rendered:
    n = len(spec.events)
    step, left, mid = 120.0, 60.0, 120.0
    width, height = left * 2 + (n - 1) * step, mid * 2
    body, boxes, labels = [f'<path class="vz-axis" d="M{left - 30} {mid} L{width - left + 30} {mid}"/>'], [], []
    for i, e in enumerate(spec.events):
        x, up = left + i * step, i % 2 == 0
        lines = wrap(e.label, 14, 3)
        body.append(f'<circle class="vz-node" cx="{x:.1f}" cy="{mid}" r="7"/>')
        tip = mid - 30 if up else mid + 30
        body.append(f'<path class="vz-edge" d="M{x:.1f} {mid + (-7 if up else 7)} L{x:.1f} {tip}"/>')
        cy = mid - 72 if up else mid + 80                      # label block, then the date between it and the connector
        body.append(text_block(x, cy, lines))
        body.append(f'<text x="{x:.1f}" y="{mid - 38 if up else mid + 46}" text-anchor="middle" class="vz-small">{esc(e.when)}</text>')
        boxes.append((x - 55, cy - len(lines) * LINE_H / 2, 110, len(lines) * LINE_H, e.label))
        labels += [e.label, str(e.when)]
    return wrap_svg(width, height, "".join(body), spec.title, spec.alt_text, boxes, labels)


# ==================================================================================== plot
def render_plot(spec: PlotSpec) -> Rendered:
    W, H, L, R, T, B = 520.0, 330.0, 62.0, 24.0, 36.0, 56.0
    pts = safeexpr.sample(spec.fn, spec.x_range)
    ys = [y for _, y in pts if y is not None]
    if spec.y_range:
        y0, y1 = spec.y_range
    elif ys:
        lo, hi = min(ys), max(ys)
        pad = (hi - lo) * 0.08 or 1.0
        y0, y1 = lo - pad, hi + pad
    else:
        y0, y1 = -1.0, 1.0
    y0, y1 = max(y0, -1e6), min(y1, 1e6)
    x0, x1 = spec.x_range
    sx = lambda x: L + (x - x0) / (x1 - x0) * (W - L - R)  # noqa: E731
    sy = lambda y: T + (1 - (y - y0) / (y1 - y0)) * (H - T - B)  # noqa: E731
    body, labels = [], [spec.labels.x, spec.labels.y]
    fmt = lambda v: f"{v:.3g}"  # noqa: E731
    for i in range(6):
        gx, gy = x0 + (x1 - x0) * i / 5, y0 + (y1 - y0) * i / 5
        body.append(f'<path class="vz-grid" d="M{sx(gx):.1f} {T} L{sx(gx):.1f} {H - B}"/><path class="vz-grid" d="M{L} {sy(gy):.1f} L{W - R} {sy(gy):.1f}"/>')
        body.append(f'<text x="{sx(gx):.1f}" y="{H - B + 16}" text-anchor="middle" class="vz-small">{fmt(gx)}</text>')
        body.append(f'<text x="{L - 8}" y="{sy(gy) + 4:.1f}" text-anchor="end" class="vz-small">{fmt(gy)}</text>')
    ax = sx(0) if x0 <= 0 <= x1 else L
    ay = sy(0) if y0 <= 0 <= y1 else H - B
    body.append(f'<path class="vz-axis" d="M{L} {ay:.1f} L{W - R} {ay:.1f}"/><path class="vz-axis" d="M{ax:.1f} {T} L{ax:.1f} {H - B}"/>')
    seg: list[str] = []
    for x, y in pts + [(0, None)]:
        if y is not None and y0 <= y <= y1:
            seg.append(f"{sx(x):.1f} {sy(y):.1f}")
        else:
            if len(seg) > 1:
                body.append(f'<polyline class="vz-line" points="{" ".join(seg)}"/>')
            seg = []
    body.append(f'<text x="{(L + W - R) / 2:.1f}" y="{H - 14}" text-anchor="middle" class="vz-text">{esc(spec.labels.x)}</text>')
    body.append(f'<text x="16" y="{(T + H - B) / 2:.1f}" text-anchor="middle" class="vz-text" transform="rotate(-90 16 {(T + H - B) / 2:.1f})">{esc(spec.labels.y)}</text>')
    body.append(f'<text x="{(L + W - R) / 2:.1f}" y="22" text-anchor="middle" class="vz-text">{esc(spec.title)}</text>')
    return wrap_svg(W, H, "".join(body), spec.title, spec.alt_text, [], labels)


_RENDERERS = {"flowchart": render_flowchart, "array": render_array, "linked_list": render_linked_list, "stack_queue": render_stack_queue,
              "tree": render_tree, "graph": render_graph, "table": render_table, "timeline": render_timeline, "plot": render_plot}


def render(spec) -> Rendered:
    return _RENDERERS[spec.type](spec)


# ================================================================================ verify
MAX_SVG_BYTES = 80_000


def check_render(r: Rendered, spec=None) -> list[str]:
    """Problems that make a rendered diagram unfit to show (an empty list means fine): not well-formed, too big, empty labels,
    boxes outside the canvas, overlapping boxes."""
    problems: list[str] = []
    if len(r.svg.encode()) > MAX_SVG_BYTES:
        problems.append("svg is too large")
    try:
        ET.fromstring(r.svg)
    except ET.ParseError as e:
        problems.append(f"svg is not well-formed: {e}")
    if any(not str(l).strip() for l in r.labels):
        problems.append("a label is empty")
    for x, y, w, h, label in r.boxes:
        if x < -1 or y < -1 or x + w > r.width + 1 or y + h > r.height + 1:
            problems.append(f"'{label[:20]}' falls outside the canvas")
    kind = getattr(spec, "type", "")
    if kind not in ("timeline", "stack_queue", "array", "table", "linked_list"):  # adjacent cells legitimately touch
        for i, a in enumerate(r.boxes):
            for b in r.boxes[i + 1:]:
                if a[0] < b[0] + b[2] - 1 and b[0] < a[0] + a[2] - 1 and a[1] < b[1] + b[3] - 1 and b[1] < a[1] + a[3] - 1:
                    problems.append(f"'{a[4][:16]}' overlaps '{b[4][:16]}'")
    return problems
