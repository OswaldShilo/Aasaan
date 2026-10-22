"""Page text (or Markdown from an OCR reader) -> structured blocks: headings, paragraphs, lists, code, tables, contents rows.

Pure code, no model. Works on plain text (heuristics) and on Markdown with `<table>` HTML (what Indra OCR emits), so every tier feeds the same pipeline.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

TOC_WORDS = re.compile(r"\b(contents?|table of contents|syllabus|curriculum|modules?|agenda|outline|topics|course plan)\b", re.I)
LIST_RE = re.compile(r"^\s*(?:(\d+(?:\.\d+)*[.)])|([-*•●▪]))\s+(.*\S)\s*$")
HEAD_MD = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
NUMBERED_HEAD = re.compile(r"^\s*((?:\d+\.)+\d*|\d+[.)]|chapter\s+\d+[:.]?|unit\s+\d+[:.]?|module\s+\d+[:.]?)\s+\S.*$", re.I)
LEADER_PAGE = re.compile(r"\s*(?:\.{2,}|…+|_{2,}|\s{3,})\s*(\d{1,4})\s*$")
CODE_LINE = re.compile(r"^\s*(def |class |import |from \S+ import |for .*:\s*$|if .*:\s*$|elif .*:\s*$|else:\s*$|while .*:\s*$|return\b|print\(|#include|public |private |function |const |let |var |SELECT |FROM |WHERE |JOIN )|[{};]\s*$|^\s{4,}\S.*[=(]")
TABLE_CELL = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.I | re.S)
TABLE_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.I | re.S)
TABLE_HTML = re.compile(r"<table[^>]*>.*?</table>", re.I | re.S)
TAGS = re.compile(r"<[^>]+>")
URL_RE = re.compile(r"https?://[^\s<>\")\]]+")
MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


@dataclass
class Block:
    type: str                              # heading | paragraph | list | code | table | toc
    text: str = ""
    level: int = 0                         # heading level 1..6
    items: list[str] = field(default_factory=list)        # list / toc items
    columns: list[str] = field(default_factory=list)      # table
    rows: list[list[str]] = field(default_factory=list)   # table


# ------------------------------------------------------------------------------------------------ cleanup
def normalize_text(text: str) -> str:
    """Whitespace and line repair: fix hyphenation at line ends, drop form feeds and stray control characters."""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n").replace("­", "")
    t = re.sub(r"[\x00-\x08\x0b\x0e-\x1f]", "", t)
    t = re.sub(r"(\w)-\n(?=[a-zà-ɏ])", r"\1", t)                 # exam-\nple -> example
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def strip_furniture(pages: list[str]) -> tuple[list[str], list[str]]:
    """Remove running headers and footers: top/bottom lines that repeat (digits ignored) on at least half the pages (and at least 3)."""
    if len(pages) < 3:
        return pages, []
    key = lambda s: re.sub(r"\d+", "#", re.sub(r"\s+", " ", s.strip().lower()))
    seen: Counter = Counter()
    for p in pages:
        lines = [ln for ln in p.split("\n") if ln.strip()]
        for ln in {key(x) for x in lines[:2] + lines[-2:] if len(x.split()) <= 8}:      # furniture is short; a long repeated sentence is content
            seen[ln] += 1
    cut = {k for k, n in seen.items() if n >= max(3, len(pages) // 2) and k.strip("# ")}
    out, removed = [], []
    for p in pages:
        lines = p.split("\n")
        nz = [i for i, ln in enumerate(lines) if ln.strip()]
        drop = {i for i in nz[:2] + nz[-2:] if len(lines[i].split()) <= 8 and key(lines[i]) in cut}
        removed += [lines[i].strip() for i in drop]
        out.append("\n".join(ln for i, ln in enumerate(lines) if i not in drop).strip())
    return out, sorted(set(removed))


def page_hash(text: str) -> str:
    return hashlib.sha1(re.sub(r"\W+", "", text.lower()).encode()).hexdigest()


def dedupe_pages(pages: list[str]) -> list[Optional[int]]:
    """For each page, the 0-based index of an earlier identical page (long enough to matter), else None."""
    first: dict[str, int] = {}
    out: list[Optional[int]] = []
    for i, p in enumerate(pages):
        if len(p.strip()) < 60:
            out.append(None)
            continue
        h = page_hash(p)
        out.append(first.get(h))
        first.setdefault(h, i)
    return out


# ------------------------------------------------------------------------------------------------ tables
def parse_html_tables(text: str) -> tuple[str, list[tuple[list[str], list[list[str]]]]]:
    """Pull <table> blocks out of text (Indra emits them). Returns (text with a marker per table, [(columns, rows)])."""
    tables: list[tuple[list[str], list[list[str]]]] = []

    def take(m: re.Match) -> str:
        rows = [[re.sub(r"\s+", " ", TAGS.sub("", c)).strip() for c in TABLE_CELL.findall(r)] for r in TABLE_ROW.findall(m.group(0))]
        rows = [r for r in rows if any(r)]
        if not rows:
            return ""
        tables.append((rows[0], rows[1:]))
        return f"\n\n⁣TABLE{len(tables) - 1}⁣\n\n"
    return TABLE_HTML.sub(take, text), tables


def _md_table(lines: list[str]) -> Optional[tuple[list[str], list[list[str]]]]:
    if len(lines) < 2 or not all("|" in ln for ln in lines):
        return None
    cells = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in lines]
    body = [r for r in cells if not all(re.fullmatch(r":?-{2,}:?", c) for c in r if c)]
    return (body[0], body[1:]) if len(body) >= 1 and len(body[0]) >= 2 else None


def _gap_table(lines: list[str]) -> Optional[tuple[list[str], list[list[str]]]]:
    """Plain-text columns separated by tabs or runs of 3+ spaces, with the same number of cells on 3+ lines."""
    rows = [re.split(r"\t+|\s{3,}", ln.strip()) for ln in lines]
    n = len(rows[0])
    if len(rows) >= 3 and n >= 2 and all(len(r) == n for r in rows) and not any(LEADER_PAGE.search(ln) for ln in lines):
        return rows[0], rows[1:]
    return None


# ------------------------------------------------------------------------------------------------ blocks
def clean_item(text: str) -> str:
    """A contents row without its number, dotted leader or page number: '1. Arrays and memory ..... 3' -> 'Arrays and memory'."""
    t = LEADER_PAGE.sub("", text.strip())
    t = re.sub(r"^\s*(?:(?:\d+\.)+\d*|\d+[.)]|[-*•●▪]|(?:chapter|unit|module)\s+\d+[:.]?)\s+", "", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip(" .:-")


def _heading_level(line: str) -> int:
    m = re.match(r"^\s*((?:\d+\.)+\d*)", line)
    return min(6, 1 + m.group(1).rstrip(".").count(".") + 1) if m else 2


def _is_plain_heading(line: str, prev_ok: bool, next_line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 80 or s[-1] in ".,;:!?" or LIST_RE.match(line) and not NUMBERED_HEAD.match(line):
        return False
    if LEADER_PAGE.search(s):
        return False
    words = s.split()
    if NUMBERED_HEAD.match(line):
        return len(words) <= 12
    if not re.fullmatch(r"[\w\s'’&,\-]+", s):          # a heading is words: parentheses, digits-with-symbols and operators mean code or data
        return False
    caps = s.isupper() and len(s) >= 4 and len(words) <= 8
    title = len(words) <= 7 and all(w[:1].isupper() or not w[:1].isalpha() or w.lower() in {"and", "of", "the", "to", "in", "for", "a", "an", "on", "with"} for w in words) and len(words) >= 2
    return (caps or title) and prev_ok and (not next_line.strip() or len(next_line.strip()) > 20 or bool(LIST_RE.match(next_line)))


def parse_page(raw: str, markdown: bool = False) -> list[Block]:
    """Structure one page. `markdown=True` trusts '#' headings and '|' tables; plain text relies on heuristics."""
    text, html_tables = parse_html_tables(normalize_text(raw))
    lines = text.split("\n")
    blocks: list[Block] = []
    para: list[str] = []
    i, n = 0, len(lines)

    def flush() -> None:
        if para:
            joined = re.sub(r"\s+", " ", " ".join(para)).strip()
            if joined:
                blocks.append(Block("paragraph", joined))
            para.clear()

    while i < n:
        ln = lines[i]
        s = ln.strip()
        if not s:
            flush()
            i += 1
            continue
        tm = re.fullmatch("⁣TABLE(\\d+)⁣", s)
        if tm:
            flush()
            cols, rows = html_tables[int(tm.group(1))]
            blocks.append(Block("table", columns=cols, rows=rows))
            i += 1
            continue
        if s.startswith("```"):                                          # fenced code
            flush()
            j = i + 1
            while j < n and not lines[j].strip().startswith("```"):
                j += 1
            blocks.append(Block("code", "\n".join(lines[i + 1:j])))
            i = j + 1
            continue
        hm = HEAD_MD.match(ln)
        if hm:
            flush()
            blocks.append(Block("heading", hm.group(2).strip(), level=len(hm.group(1))))
            i += 1
            continue
        if "|" in s:
            j = i
            while j < n and "|" in lines[j]:
                j += 1
            t = _md_table(lines[i:j])
            if t:
                flush()
                blocks.append(Block("table", columns=t[0], rows=t[1]))
                i = j
                continue
        if LIST_RE.match(ln):
            j = i
            items: list[str] = []
            while j < n and lines[j].strip():
                m = LIST_RE.match(lines[j])
                if m:
                    items.append(m.group(3).strip())
                elif items and lines[j].startswith(" ") and not CODE_LINE.match(lines[j]):
                    items[-1] += " " + lines[j].strip()                  # a wrapped list item
                else:
                    break
                j += 1
            if len(items) >= 3 or any(LEADER_PAGE.search(x) for x in items) or (len(items) >= 2 and not NUMBERED_HEAD.match(ln)):
                flush()
                is_toc = any(LEADER_PAGE.search(x) for x in items) or bool(blocks and blocks[-1].type == "heading" and TOC_WORDS.search(blocks[-1].text))
                blocks.append(Block("toc" if is_toc else "list", items=items))
                i = j
                continue
        if CODE_LINE.match(ln):
            j = i
            while j < n and lines[j].strip() and (CODE_LINE.match(lines[j]) or lines[j].startswith("    ")):
                j += 1
            if j - i >= 2:
                flush()
                blocks.append(Block("code", "\n".join(lines[i:j])))
                i = j
                continue
        if not markdown:                                                 # a reader that returns Markdown already marked its headings
            nxt = lines[i + 1] if i + 1 < n else ""
            prev = lines[i - 1].strip() if i else ""
            if _is_plain_heading(ln, prev_ok=(not prev or prev[-1] in ".!?:)”\"'" or len(prev) < 40), next_line=nxt):
                flush()
                blocks.append(Block("heading", s, level=_heading_level(ln)))
                i += 1
                continue
        if "\t" in ln or re.search(r"\S\s{3,}\S", ln):                 # candidate plain-text table
            j = i
            while j < n and lines[j].strip() and ("\t" in lines[j] or re.search(r"\S\s{3,}\S", lines[j])):
                j += 1
            t = _gap_table(lines[i:j])
            if t:
                flush()
                blocks.append(Block("table", columns=t[0], rows=t[1]))
                i = j
                continue
        para.append(s)
        i += 1
    flush()
    return blocks


def links_in(text: str) -> list[tuple[str, str]]:
    """(url, anchor text) pairs found in text; Markdown links keep their anchor."""
    found: dict[str, str] = {}
    for a, u in MD_LINK.findall(text or ""):
        found.setdefault(u.rstrip(".,;:!?"), a.strip())
    for u in URL_RE.findall(text or ""):
        found.setdefault(u.rstrip(".,;:!?"), "")
    return list(found.items())
