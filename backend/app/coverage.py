"""Does the outline cover what the learner's own contents list or syllabus asks for? Pure code: no model, no network.

`checklist_from_chunks` finds an ordered list of items in the learner's material (a contents list, syllabus rows, numbered topics),
`coverage_map` matches each item to the chapter that best covers it and reports the gaps, `validate_outline` flags structural problems.
"""
from __future__ import annotations

import re
from typing import Optional

from .agents.common import overlap, tokens
from .intake.structure import clean_item, parse_page

NUMBERED = re.compile(r"(?:^|\s)(\d{1,2})[.)]\s+(.{3,70}?)(?=\s+\d{1,2}[.)]\s|\s*$)")
MIN_ITEMS = 4


def _numbered_run(text: str) -> list[str]:
    """'1. Arrays 2. Linked lists 3. Trees 4. Hashing' (a list flattened onto one line) -> items, only if the numbers run 1, 2, 3, ..."""
    found = [(int(n), t.strip()) for n, t in NUMBERED.findall(text)]
    items, want = [], 1
    for n, t in found:
        if n == want:
            items.append(clean_item(t))
            want += 1
    return items if len(items) >= MIN_ITEMS else []


def checklist_from_chunks(chunks: list[dict]) -> list[str]:
    """The learner's own contents rows, in order. Looks at contents/list blocks first, then at flattened numbered runs. Empty when there is none."""
    items: list[str] = []
    for c in chunks:
        text = c.get("text", "")
        for blk in parse_page(text):
            if blk.type == "toc" or (blk.type == "list" and len(blk.items) >= MIN_ITEMS):
                items += [clean_item(i) for i in blk.items]
    if len(items) < MIN_ITEMS:
        for c in chunks:
            run = _numbered_run(c.get("text", ""))
            if run:
                items = run
                break
    seen, out = set(), []
    for i in items:
        if i and i.lower() not in seen:
            seen.add(i.lower())
            out.append(i)
    return out if len(out) >= MIN_ITEMS else []


def coverage_map(items: list[str], chapters: list[dict]) -> dict:
    """{"map": [{item, chapter_id, position, score}], "gaps": [item, ...]}: each item goes to the chapter whose title, summary and objectives share the most words."""
    rows, gaps = [], []
    for item in items:
        words = tokens(item, 3)
        best: Optional[dict] = None
        best_score = 0
        for ch in chapters:
            sc = overlap(words, f"{ch['title']} {ch.get('summary', '')} {' '.join(ch.get('objectives', []))}") + 2 * overlap(words, ch["title"])
            if sc > best_score:
                best, best_score = ch, sc
        if best is not None and words and best_score >= 1:
            rows.append({"item": item, "chapter_id": best["id"], "position": best["position"], "score": best_score})
        else:
            gaps.append(item)
    return {"map": rows, "gaps": gaps}


def validate_outline(chapters: list[dict]) -> list[str]:
    """Structural problems worth reporting (the outline is kept either way): missing objectives, a prerequisite that points later, a missing title."""
    problems = []
    pos = {c["id"]: c["position"] for c in chapters}
    for c in chapters:
        if not c.get("title", "").strip():
            problems.append(f"chapter {c['position']} has no title")
        if not c.get("objectives"):
            problems.append(f"chapter {c['position']} has no objectives")
        if any(pos.get(p, 0) >= c["position"] for p in c.get("prereqs", [])):
            problems.append(f"chapter {c['position']} depends on a later chapter")
    return problems
