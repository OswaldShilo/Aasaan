"""Python snippet -> flowchart spec using the `ast` module. No model is involved.

Handles if/elif/else, for, while, return, break, continue and plain statements. Runs of simple statements collapse into one
box. If the result would exceed the node cap, the code is too big for one diagram and we return None (the caller falls back).
"""
from __future__ import annotations

import ast
import re
import textwrap
from typing import Optional

from .specs import MAX_LABEL_WORDS, MAX_NODES, FlowchartSpec, spec_dict

CODE_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S | re.I)


class TooBig(Exception):
    pass


MAX_HIGH_NODES = 12                    # the high-level chart shows the ideas only (setup, loops, decisions, returns); typical snippets come to about 10
OPS = {ast.Eq: "equals", ast.NotEq: "differs from", ast.Lt: "is below", ast.LtE: "is at most", ast.Gt: "is above", ast.GtE: "is at least",
       ast.In: "is in", ast.NotIn: "is not in", ast.Is: "is", ast.IsNot: "is not"}


def _short(text: str, words: int = MAX_LABEL_WORDS) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    parts = text.split(" ")
    if len(parts) > words:
        text = " ".join(parts[:words])
    return text[:34]            # the renderer wraps a label into two lines of about 15 characters


def _src(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001
        return "step"


def _plain_test(e: ast.expr, loop: bool = False) -> str:
    """A condition in plain English: `low <= high` -> `low is at most high`, `a[mid] == x` -> `a[mid] equals x`."""
    if isinstance(e, ast.Compare) and len(e.ops) == 1 and type(e.ops[0]) in OPS:
        return f"{_src(e.left)} {OPS[type(e.ops[0])]} {_src(e.comparators[0])}"
    if isinstance(e, ast.BoolOp):
        return (" and " if isinstance(e.op, ast.And) else " or ").join(_plain_test(v) for v in e.values)
    if isinstance(e, ast.UnaryOp) and isinstance(e.op, ast.Not):
        return "not " + _plain_test(e.operand)
    return _src(e)


def _plain_stmt(s: ast.stmt) -> str:
    """A statement as a short verb phrase: `mid = (low + high) // 2` -> `Work out mid`, `low = mid + 1` -> `Update low`."""
    if isinstance(s, ast.Assign):
        t = _src(s.targets[0])
        return f"Set {t}" if isinstance(s.value, (ast.Constant, ast.Name, ast.List, ast.Tuple, ast.Dict)) else f"Work out {t}"
    if isinstance(s, ast.AnnAssign):
        return f"Set {_src(s.target)}"
    if isinstance(s, ast.AugAssign):
        return f"Update {_src(s.target)}"
    if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call):
        name = _src(s.value.func)
        return "Show output" if name == "print" else (f"Add to {name.rsplit('.', 1)[0]}" if name.endswith(".append") else f"Run {name}")
    if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return f"Define {s.name}"
    return "Do a step"


def _stmt_label(s: ast.stmt) -> str:
    if isinstance(s, ast.Assign):
        return _short(f"{_src(s.targets[0])} = {_src(s.value)}")
    if isinstance(s, ast.AugAssign):
        return _short(_src(s))
    if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call):
        name = _src(s.value.func)
        return _short(("print output" if name == "print" else f"call {name}"))
    if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return _short(f"define {s.name}")
    return _short(_src(s).split("\n")[0])


class Builder:
    def __init__(self, high: bool = False):
        self.nodes: list[dict] = []
        self.edges: list[dict] = []
        self.high = high                                   # plain-English labels and bigger collapsing, at most MAX_HIGH_NODES nodes
        self.lines: dict[str, list[int]] = {}              # node id -> [first, last] source line, so the page can highlight the code

    def node(self, label: str, shape: str = "process", lines: Optional[tuple[int, int]] = None) -> str:
        if len(self.nodes) >= (MAX_HIGH_NODES if self.high else MAX_NODES):
            raise TooBig()
        nid = f"n{len(self.nodes) + 1}"
        self.nodes.append({"id": nid, "label": label or "step", "shape": shape})
        if lines:
            self.lines[nid] = [lines[0], lines[1]]
        return nid

    def edge(self, a: str, b: str, label: Optional[str] = None):
        e = {"from": a, "to": b}
        if label:
            e["label"] = label
        if not any(x["from"] == a and x["to"] == b and x.get("label") == label for x in self.edges):
            self.edges.append(e)

    def block(self, stmts: list[ast.stmt], entries: list[tuple[str, Optional[str]]], loop: Optional[tuple[str, list]] = None, end: Optional[str] = None):
        """Add `stmts` after `entries` ([(node, edge label)]); return the dangling exits in the same form."""
        run: list[ast.stmt] = []

        def flush():
            nonlocal entries, run
            if not run:
                return
            if self.high:
                names = list(dict.fromkeys(_plain_stmt(x) for x in run))
                label = _short(names[0] if len(names) == 1 else f"{names[0]} and {len(names) - 1} more" if len(names) > 2 else f"{names[0]}, {names[1][0].lower() + names[1][1:]}")
            else:
                label = _stmt_label(run[0]) if len(run) == 1 else _short(f"{_stmt_label(run[0])} +{len(run) - 1} more")
            n = self.node(label, lines=(run[0].lineno, getattr(run[-1], "end_lineno", run[-1].lineno)))
            for a, lab in entries:
                self.edge(a, n, lab)
            entries, run = [(n, None)], []

        for s in stmts:
            if isinstance(s, (ast.Import, ast.ImportFrom, ast.Pass)) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)):
                continue
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                entries = self.block(s.body, entries, loop, end)  # draw the function body inline
                continue
            if isinstance(s, ast.If):
                flush()
                d = self.node(_short(_plain_test(s.test) if self.high else _src(s.test)) + "?", "decision", (s.lineno, s.test.end_lineno or s.lineno))
                for a, lab in entries:
                    self.edge(a, d, lab)
                yes = self.block(s.body, [(d, "yes")], loop, end)
                no = self.block(s.orelse, [(d, "no")], loop, end) if s.orelse else [(d, "no")]
                entries = yes + no
            elif isinstance(s, (ast.For, ast.While)):
                flush()
                if self.high:
                    test = f"Next {_src(s.target)} in {_src(s.iter)}" if isinstance(s, ast.For) else f"Repeat while {_src(s.test)}"
                else:
                    test = f"more in {_src(s.target)}" if isinstance(s, ast.For) else _src(s.test)
                d = self.node(_short(test) + "?", "decision", (s.lineno, (s.test.end_lineno if isinstance(s, ast.While) else s.iter.end_lineno) or s.lineno))
                for a, lab in entries:
                    self.edge(a, d, lab)
                breaks: list = []
                tail = self.block(s.body, [(d, "yes")], (d, breaks), end)
                for a, lab in tail:
                    self.edge(a, d, lab)
                entries = [(d, "no")] + breaks
            elif isinstance(s, ast.Return):
                flush()
                n = self.node(_short(("Give back " + _src(s.value)) if (self.high and s.value) else "return " + (_src(s.value) if s.value else "")), "io", (s.lineno, s.end_lineno or s.lineno))
                for a, lab in entries:
                    self.edge(a, n, lab)
                if end:
                    self.edge(n, end)
                entries = []
            elif isinstance(s, ast.Break) and loop:
                flush()
                loop[1].extend(entries)
                entries = []
            elif isinstance(s, ast.Continue) and loop:
                flush()
                for a, lab in entries:
                    self.edge(a, loop[0], lab)
                entries = []
            else:
                run.append(s)
            if not entries and not run:
                break
        flush()
        return entries


def extract_python(text: str) -> list[str]:
    """Python code blocks from markdown notes (fenced, or a whole snippet that parses)."""
    out = [m.group(1) for m in CODE_RE.finditer(text or "")]
    return [textwrap.dedent(c).strip("\n") for c in out if c.strip()]


def code_to_flowchart(code: str, title: str = "How the code runs", high: bool = False) -> Optional[dict]:
    """A validated flowchart spec dict for the code, or None when it can't be drawn (syntax error, nothing to draw, too big)."""
    got = _build(code, title, high)
    return got[0] if got else None


def code_to_levels(code: str, title: str = "How the code runs") -> Optional[dict]:
    """Both views of one snippet: {"high": spec, "detailed": spec, "lines": {"high": {node: [a, b]}, "detailed": {...}}, "code": dedented code}.
    High level (default) is at most 10 plain-English nodes; detailed keeps the code's own wording. A missing view is None; both missing -> None."""
    code = textwrap.dedent(code).strip("\n")
    hi, de = _build(code, title, True), _build(code, title, False)
    if not hi and not de:
        return None
    return {"high": hi[0] if hi else None, "detailed": de[0] if de else None, "lines": {"high": hi[1] if hi else {}, "detailed": de[1] if de else {}}, "code": code}


def _build(code: str, title: str, high: bool) -> Optional[tuple[dict, dict]]:
    try:
        tree = ast.parse(textwrap.dedent(code))
    except SyntaxError:
        return None
    b = Builder(high)
    try:
        start = b.node("Start", "start")
        end = b.node("End", "end")
        exits = b.block(tree.body, [(start, None)], None, end)
        if len(b.nodes) <= 2:
            return None
        for a, lab in exits:
            b.edge(a, end, lab)
        spec = FlowchartSpec.model_validate({"type": "flowchart", "title": _short(title, 8) or "Code flow", "nodes": b.nodes, "edges": b.edges,
                                             "alt_text": ("High-level flowchart of the code: " if high else "Flowchart of the code: ") + "; ".join(n["label"] for n in b.nodes[2:8])})
    except (TooBig, ValueError):
        return None
    reachable = {"n1"}
    for _ in range(len(b.nodes)):
        for e in b.edges:
            if e["from"] in reachable:
                reachable.add(e["to"])
    if len(reachable) < len(b.nodes):  # dead code after a return: keep only what is reachable
        keep = [n for n in b.nodes if n["id"] in reachable]
        edges = [e for e in b.edges if e["from"] in reachable and e["to"] in reachable]
        try:
            spec = FlowchartSpec.model_validate({**spec_dict(spec), "nodes": keep, "edges": edges})
        except ValueError:
            return None
    kept = {n.id for n in spec.nodes}
    return spec_dict(spec), {k: v for k, v in b.lines.items() if k in kept}
