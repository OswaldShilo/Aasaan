"""Illustrator: owns every visual in a chapter. Code draws; models only decide and fill small JSON specs.

Per section of the approved notes (one batched decision for all sections, never one call each):
  1. decide: does a picture help (Noul) and which kind (Choice)? Jev -> Jev via OpenRouter -> Mercury Decide -> Gemma -> rules
     (rules: a Python snippet gives a code flowchart, otherwise no visual).
  2. build, cheapest first:
       code_flowchart  Python AST -> flowchart spec, NO model call
       spec kinds      Gemma 26B/31B/Sarvam fill a JSON spec (schema-validated, one repair, then the next model); code renders the SVG
       diagram         free-form Mermaid, parse-checked (one repair) and converted to the nearest flowchart spec
       figure          Commons (SVG preferred, licence check) -> Openverse -> nothing. Real figures are never drawn by a model.
  3. verify: schema, render, text fit, alt text + caption, then ONE batched faithfulness gate (low confidence drops the visual).
At most MAX_VISUALS per chapter. A visual that fails is dropped with an event saying why; it never blocks the chapter.
Results are cached by hash(topic, kind, section) and reused across notebooks.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Optional

from ..providers.typesafe import choice, noul
from ..router import ModelRouter
from ..visuals import astflow, mermaid, render, specs
from ..visuals.figures import find_figure
from .common import build_prompt

MAX_VISUALS, MAX_SECTIONS = 3, 6
FAITHFUL_MIN = 0.4
KIND_HELP = {
    "code_flowchart": "the section has a Python code snippet whose control flow can be drawn",
    "flowchart": "a process or algorithm with steps and decisions",
    "array": "a list/array of values, indexes or pointers",
    "linked_list": "nodes linked by pointers",
    "stack_queue": "a stack (LIFO) or queue (FIFO) with items",
    "tree": "a hierarchy: tree, heap, parse tree, taxonomy",
    "graph": "nodes and edges: a network, map of connections, dependencies",
    "table": "a small comparison or reference table",
    "timeline": "events in time order",
    "plot": "the graph of a function y = f(x)",
    "diagram": "some other simple box-and-arrow diagram",
    "figure": "a real-world picture (anatomy, map, apparatus, photo, circuit) that must be accurate",
    "none": "no picture needed",
}
PHYSICAL = re.compile(r"\b(anatomy|cell|organ|circuit|ray diagram|lens|mirror|molecule|atom|orbital|map|apparatus|skeleton|heart|brain|plant|flower|"
                      r"force diagram|free body|magnet|wave|optic|planet|solar system|rock|volcano|periodic)\w*", re.I)
EXAMPLES = {
    "flowchart": {"type": "flowchart", "title": "Binary search", "alt_text": "Flowchart of binary search",
                  "nodes": [{"id": "a", "label": "Start", "shape": "start"}, {"id": "b", "label": "mid equals target?", "shape": "decision"}, {"id": "c", "label": "Done", "shape": "end"}],
                  "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "c", "label": "yes"}, {"from": "b", "to": "a", "label": "no"}]},
    "array": {"type": "array", "title": "Array of numbers", "alt_text": "Array with pointer i", "values": [4, 8, 15], "highlight": [1], "pointers": [{"name": "i", "index": 1}]},
    "linked_list": {"type": "linked_list", "title": "A linked list", "alt_text": "Three linked nodes", "values": [1, 2, 3], "doubly": False, "head_label": "head"},
    "stack_queue": {"type": "stack_queue", "title": "A stack", "alt_text": "Stack with three items", "kind": "stack", "items": [1, 2, 3], "operation": "push 4"},
    "tree": {"type": "tree", "title": "A binary tree", "alt_text": "Root with two children", "nodes": [{"id": "r", "label": "8"}, {"id": "a", "label": "3"}, {"id": "b", "label": "10"}],
             "edges": [{"parent": "r", "child": "a"}, {"parent": "r", "child": "b"}], "highlight": []},
    "graph": {"type": "graph", "title": "A small graph", "alt_text": "Three connected nodes", "nodes": ["A", "B", "C"], "edges": [{"from": "A", "to": "B", "weight": 2}, {"from": "B", "to": "C"}], "directed": False},
    "table": {"type": "table", "title": "Operation costs", "alt_text": "Costs of two operations", "columns": ["Operation", "Cost"], "rows": [["push", "O(1)"], ["pop", "O(1)"]]},
    "timeline": {"type": "timeline", "title": "Key events", "alt_text": "Three events in order", "events": [{"label": "First event", "when": 1900}, {"label": "Second event", "when": 1950}]},
    "plot": {"type": "plot", "title": "y equals x squared", "alt_text": "Parabola", "fn": "x^2", "x_range": [-3, 3], "labels": {"x": "x", "y": "y"}},
}
NO_SPEC = object()   # last resort of the illustrator chain: no model could fill a valid spec


@dataclass
class Section:
    id: str
    heading: str
    text: str
    code: Optional[str] = None


@dataclass
class IllustratorOut:
    blocks: list[dict] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def split_sections(notes_md: str) -> list[Section]:
    parts = re.split(r"(?m)^#{2,3}\s+(.+)$", notes_md or "")
    out: list[Section] = []
    if len(parts) == 1:
        paras = [p.strip() for p in re.split(r"\n\s*\n", notes_md or "") if p.strip()]
        parts = [""] + [x for p in paras for x in (p.split("\n", 1)[0][:50], p)]
    for i in range(1, len(parts) - 1, 2):
        heading, body = parts[i].strip(), parts[i + 1].strip()
        if len(body) < 40:
            continue
        codes = astflow.extract_python(body)
        out.append(Section(f"s{len(out)}", heading, re.sub(r"```.*?```", " [code] ", body, flags=re.S)[:700], codes[0] if codes else None))
        if len(out) >= MAX_SECTIONS:
            break
    return out


def _h(*parts) -> str:
    return hashlib.sha1("|".join(str(p).strip().lower() for p in parts).encode()).hexdigest()[:20]


def decide_kinds(router: ModelRouter, topic: str, secs: list[Section]) -> dict[str, dict]:
    """{section id: {"kind", "by"}} for sections that should get a visual."""
    state = {"chapter": topic, "sections": {s.id: {"heading": s.heading, "text": s.text[:350], "has_python_code": bool(s.code)} for s in secs}}
    questions = {}
    for s in secs:
        questions[f"v_{s.id}"] = noul(f"Would a picture or diagram genuinely help a student understand the section '{s.heading}'?",
                                      "A diagram would clarify a process, structure or relationship that text explains poorly",
                                      "Plain prose is enough, or a picture would only decorate")
        opts = {k: v for k, v in KIND_HELP.items() if k != "code_flowchart" or s.code}
        questions[f"k_{s.id}"] = choice(f"Which kind of picture suits the section '{s.heading}' best?", opts)
    answers = router.decide("illustrator", state, questions, student_data=False, summary=f"visual decisions for '{topic[:40]}'")
    out: dict[str, dict] = {}
    if not answers:
        router.note("illustrator", "fallback", "visual decisions by rules", "no decision provider answered; code snippets get a flowchart, other sections none")
        for s in secs:
            if s.code:
                out[s.id] = {"kind": "code_flowchart", "by": "rules"}
        return out
    for s in secs:
        v, k = answers.get(f"v_{s.id}"), answers.get(f"k_{s.id}")
        try:
            wants = v is not None and float(v["noul"]) >= 0.5
            kind = k["choice"] if k else None
        except (KeyError, TypeError, ValueError):
            continue
        if wants and kind and kind != "none" and kind in KIND_HELP:
            if kind == "code_flowchart" and not s.code:
                kind = "flowchart"
            out[s.id] = {"kind": kind, "by": (k or v).get("_by", "?")}
        elif s.code and (v is None or wants or kind in (None, "flowchart", "code_flowchart")):
            out[s.id] = {"kind": "code_flowchart", "by": (k or v or {}).get("_by", "rules")}
    return out


# ----------------------------------------------------------------------------- builders
def _prompt_spec(kind: str, topic: str, s: Section) -> str:
    return build_prompt(
        f"You are Illustrator. Fill a small JSON spec for a {kind} that helps a student understand this section. You only fill the JSON: code draws it. "
        "Use at most 12 nodes (16 elements), labels of at most 5 words, and give a short title and alt_text. Decision nodes need exactly two labelled outgoing edges.",
        EXAMPLES[kind], {"chapter": topic, "section": s.heading, "text": s.text})


def _prompt_mermaid(topic: str, s: Section) -> str:
    return build_prompt(
        "You are Illustrator. Draw a small flowchart for this section as Mermaid text (`flowchart TD`, nodes like A[text] or B{question}, edges `A --> B` or `A -->|label| B`). "
        "At most 12 nodes, labels of at most 5 words, no subgraphs.",
        {"mermaid": "flowchart TD\nA([Start]) --> B{Done?}\nB -->|yes| C([End])\nB -->|no| A", "title": "short title", "alt_text": "one sentence describing the picture"},
        {"chapter": topic, "section": s.heading, "text": s.text})


def _fit(spec) -> specs.BaseModel:
    r = render.render(spec)
    problems = render.check_render(r, spec)
    if problems:
        raise ValueError("; ".join(problems[:3]))
    return r


def model_spec(router: ModelRouter, kind: str, topic: str, s: Section, language: str) -> Optional[dict]:
    """A validated spec dict from the model ladder (each model gets one repair), or None when every tier failed."""
    if kind == "diagram":
        def validate(raw):
            m = raw.get("mermaid") if isinstance(raw, dict) else None
            if not isinstance(m, str):
                raise ValueError("reply must be {\"mermaid\": \"flowchart TD ...\", \"title\": ..., \"alt_text\": ...}")
            d = mermaid.parse(m, str(raw.get("title") or s.heading)[:60], str(raw.get("alt_text") or ""))
            _fit(specs.parse_spec(d))
            return d
        prompt, ctx = _prompt_mermaid(topic, s), {"kind": "diagram", "section": s.heading, "text": s.text}
    else:
        def validate(raw):
            d = raw.get("spec", raw) if isinstance(raw, dict) else raw
            if isinstance(d, dict) and "type" not in d:
                d = {**d, "type": kind}
            spec = specs.parse_spec(d)
            if spec.type != kind:
                raise ValueError(f"asked for a {kind} but got a {spec.type}")
            _fit(spec)
            return specs.spec_dict(spec)
        prompt, ctx = _prompt_spec(kind, topic, s), {"kind": kind, "section": s.heading, "text": s.text}
    res = router.call("illustrator", prompt, ctx, validate, last_resort=lambda c: NO_SPEC, summary=f"{kind} spec for '{s.heading[:40]}'", language=language)
    return None if res.value is NO_SPEC else res.value


def _svg_block(spec_d: dict, kind: str, tool: str, heading: str) -> dict:
    spec = specs.parse_spec(spec_d)
    r = _fit(spec)
    return {"type": "visual", "verified": True, "content": {
        "kind": spec.type if kind in ("code_flowchart", "diagram") else kind, "payload": {"svg": r.svg, "spec": spec_d}, "alt_text": spec.alt_text,
        "caption": spec.title, "attribution": None, "source_tool": tool, "section": heading}}


def _figure_block(s: Section, topic: str, router: ModelRouter) -> Optional[dict]:
    q = f"{s.heading} {topic}".strip()[:100]
    res = find_figure(q, topic)
    if res.figure is None:
        router.note("illustrator", "fallback", f"no figure for '{s.heading[:40]}'", "; ".join(dict.fromkeys(res.tried))[:500])
        return None
    f = res.figure
    return {"type": "visual", "verified": True, "content": {
        "kind": "figure", "payload": {"url": f.url, "page_url": f.page_url}, "alt_text": f"{f.title}. Figure from {f.source.title()}.", "caption": f.title,
        "attribution": {"author": f.author, "license": f.license, "source": "Wikimedia Commons" if f.source == "commons" else "Openverse", "page_url": f.page_url},
        "source_tool": f.source, "section": s.heading}}


def build_one(router: ModelRouter, store, kind: str, topic: str, s: Section, language: str) -> tuple[Optional[dict], str]:
    """(block or None, how) for one section. `how` goes into the event."""
    if kind == "code_flowchart":
        lv = astflow.code_to_levels(s.code or "", f"{s.heading} flow")
        if lv is None:
            return None, "the code could not be drawn (syntax error or more than 12 steps)"
        first = "high" if lv["high"] else "detailed"
        blk = _svg_block(lv[first], kind, "ast_flowchart", s.heading)
        blk["content"]["payload"].update({"level": first, "code": lv["code"], "lines": lv["lines"][first],
                                          "levels": {name: {"svg": render.render(specs.parse_spec(lv[name])).svg, "lines": lv["lines"][name], "alt_text": lv[name]["alt_text"]}
                                                     for name in ("high", "detailed") if lv[name]}})
        return blk, "Python AST flowchart (high level and detailed), no model call"
    if kind == "figure":
        blk = _figure_block(s, topic, router)
        return blk, "real figure" if blk else "no licensed figure found"
    if kind not in EXAMPLES and kind != "diagram":
        return None, f"unknown kind {kind}"
    if PHYSICAL.search(f"{topic} {s.heading}") and kind == "diagram":
        blk = _figure_block(s, topic, router)   # accurate physical/biological figures are never model-drawn
        return blk, "physical figure: fetched, never drawn by a model" if blk else "physical figure not found; not model-drawn"
    d = model_spec(router, kind, topic, s, language)
    if d is None:
        return None, "no model could fill a valid spec"
    return _svg_block(d, kind, "mermaid_checked" if kind == "diagram" else "spec_renderer", s.heading), f"{kind} spec filled by a model, drawn by code"


def faithfulness(router: ModelRouter, topic: str, blocks: list[tuple[Section, dict]]) -> list[tuple[Section, dict]]:
    """One batched Noul gate: does each picture match its section? Low confidence drops it. No answer = keep (the render checks passed)."""
    svg_blocks = [(s, b) for s, b in blocks if b["content"]["kind"] != "figure"]
    if not svg_blocks:
        return blocks
    state = {"chapter": topic, "visuals": {s.id: {"section": s.text[:400], "title": b["content"]["caption"], "alt_text": b["content"]["alt_text"],
                                                  "labels": [l for l in re.findall(r">([^<>]{2,40})<", b["content"]["payload"]["svg"]) if l.strip()][:25]}
                                           for s, b in svg_blocks}}
    qs = {s.id: noul("Does this diagram faithfully show what the section explains, with no wrong or invented content?",
                     "The labels and structure match the section", "The diagram contradicts, invents or misses the section's point") for s, _ in svg_blocks}
    ans = router.decide("illustrator_faithful", state, qs, student_data=False, summary=f"faithfulness of {len(qs)} visual(s)")
    if not ans:
        return blocks
    drop = set()
    for sid, a in ans.items():
        try:
            if float(a["noul"]) < FAITHFUL_MIN:
                drop.add(sid)
                router.note("illustrator", "fallback", f"dropped a visual for '{sid}'", f"faithfulness {float(a['noul']):.2f} below {FAITHFUL_MIN}")
        except (KeyError, TypeError, ValueError):
            continue
    return [(s, b) for s, b in blocks if s.id not in drop]


def run(router: ModelRouter, store, chapter_title: str, notes_md: str, language: str = "en") -> IllustratorOut:
    """Never raises: a visual that fails is skipped with an event, the chapter is unaffected."""
    out = IllustratorOut()
    try:
        secs = split_sections(notes_md)
        if not secs:
            return out
        wanted = decide_kinds(router, chapter_title, secs)
        built: list[tuple[Section, dict]] = []
        for s in secs:
            if len(built) >= MAX_VISUALS:
                break
            pick = wanted.get(s.id)
            if not pick:
                continue
            kind = pick["kind"]
            key = f"visual:{_h(chapter_title, kind, s.heading, s.code or '')}"
            hit = store.cache_get(key) if store else None
            if hit:
                router.note("illustrator", "ok", f"visual cache hit: {kind} for '{s.heading[:40]}'", "reused from an earlier notebook")
                built.append((s, hit))
                continue
            try:
                blk, how = build_one(router, store, kind, chapter_title, s, language)
            except Exception as e:  # noqa: BLE001  one bad visual never blocks the chapter
                blk, how = None, f"{type(e).__name__}: {e}"
            if blk is None:
                out.skipped.append(f"{s.heading}: {how}")
                router.note("illustrator", "fallback", f"no visual for '{s.heading[:40]}' ({kind})", how)
                continue
            router.note("illustrator", "ok", f"visual: {kind} for '{s.heading[:40]}' via {blk['content']['source_tool']}", f"{how}; decided by {pick['by']}")
            built.append((s, blk))
        built = faithfulness(router, chapter_title, built)
        for s, b in built:
            if store:
                store.cache_set(f"visual:{_h(chapter_title, wanted[s.id]['kind'], s.heading, s.code or '')}", b)
            out.blocks.append(b)
    except Exception as e:  # noqa: BLE001
        router.note("illustrator", "failed", "Illustrator failed; chapter ships without visuals", f"{type(e).__name__}: {e}")
    return out
