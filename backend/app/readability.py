"""Deterministic clean-up of Teacher notes, and the typed-section renderer. Pure code: the model decides WHAT to say, code decides how it looks.

clean_notes: repair a table written on one line, drop a repeated chapter-title heading, split overlong paragraphs at sentence ends, count long sentences,
compute reading time. render_sections: typed Teacher sections -> Markdown with fixed headings, so layout never depends on model-written structure.
"""
from __future__ import annotations

import re

SECTION_TITLES = {"why_it_matters": "Why this matters", "key_terms": "Key terms", "explanation": "Explanation", "worked_example": "Worked example",
                  "common_mistakes": "Common mistakes", "recap": "Recap", "go_deeper": "Go deeper"}
SECTION_ORDER = tuple(SECTION_TITLES)
MAX_PARAGRAPH_WORDS = 110
LONG_SENTENCE_WORDS = 35


def render_sections(sections: list[dict]) -> str:
    """[{kind, body}] -> Markdown in the fixed order; unknown kinds are dropped, empty bodies skipped. Key terms and mistakes become lists."""
    by = {s["kind"]: s for s in sections if s.get("kind") in SECTION_TITLES and (s.get("body") or "").strip()}
    out = []
    for kind in SECTION_ORDER:
        s = by.get(kind)
        if not s:
            continue
        body = s["body"].strip()
        if kind in ("key_terms", "common_mistakes") and not re.search(r"(?m)^\s*([-*]|\d+[.)])\s", body):
            body = "\n".join(f"- {ln.strip()}" for ln in re.split(r"\n+|;\s+", body) if ln.strip())
        out.append(f"## {SECTION_TITLES[kind]}\n\n{body}")
    return "\n\n".join(out)


def _split_paragraph(p: str) -> list[str]:
    words = p.split()
    if len(words) <= MAX_PARAGRAPH_WORDS or p.lstrip().startswith(("-", "*", "|", "#", "```", "    ")):
        return [p]
    sents = re.split(r"(?<=[.!?])\s+", p)
    out, cur = [], []
    for s in sents:
        if cur and sum(len(x.split()) for x in cur) + len(s.split()) > MAX_PARAGRAPH_WORDS * 0.7:
            out.append(" ".join(cur))
            cur = []
        cur.append(s)
    if cur:
        out.append(" ".join(cur))
    return out


def _fix_one_line_table(line: str) -> str:
    """'| a | b | |---|---| | 1 | 2 |' on one line -> a real table (only when the separator row is clearly inside the line)."""
    if line.count("|") < 8 or not re.search(r"\|\s*:?-{3,}", line):
        return line
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    cells = [c for c in cells if c]                                # on one line the row breaks show up as empty cells ("| |")
    sep = next((i for i, c in enumerate(cells) if re.fullmatch(r":?-{3,}:?", c)), None)
    if not sep:
        return line
    n = sep                                       # the header has `sep` cells, so every row does
    body = [c for c in cells if not re.fullmatch(r":?-{3,}:?", c)]
    rows = [body[i:i + n] for i in range(0, len(body), n)]
    if len(rows) < 2 or len(body) % n:
        return line
    return "\n".join(["| " + " | ".join(rows[0]) + " |", "|" + "---|" * n] + ["| " + " | ".join(r) + " |" for r in rows[1:]])


def clean_notes(md: str, title: str = "") -> tuple[str, dict]:
    text = (md or "").replace("\r\n", "\n").strip()
    lines = text.split("\n")
    while lines and re.match(r"^#{1,3}\s+" + re.escape(title.strip()) + r"\s*:?\s*$", lines[0].strip(), re.I) and title.strip():
        lines.pop(0)                                              # the page already shows the chapter title
        while lines and not lines[0].strip():
            lines.pop(0)
    in_code = False
    fixed = []
    for ln in lines:
        if ln.strip().startswith("```"):
            in_code = not in_code
        fixed.append(ln if in_code else _fix_one_line_table(ln))
    paras = re.split(r"\n\s*\n", "\n".join(fixed))
    out = []
    for p in paras:
        out += _split_paragraph(p) if "```" not in p else [p]
    cleaned = "\n\n".join(x.strip("\n") for x in out if x.strip())
    prose = re.sub(r"```.*?```", " ", cleaned, flags=re.S)
    sents = [s for s in re.split(r"(?<=[.!?])\s+", re.sub(r"[#>*|`-]", " ", prose)) if len(s.split()) >= 3]
    words = len(re.findall(r"[^\W_]+", prose, re.UNICODE))
    return cleaned, {"words": words, "reading_minutes": round(words / 200, 1), "long_sentences": sum(1 for s in sents if len(s.split()) > LONG_SENTENCE_WORDS),
                     "longest_paragraph_words": max((len(p.split()) for p in re.split(r"\n\s*\n", prose) if p.strip()), default=0)}
