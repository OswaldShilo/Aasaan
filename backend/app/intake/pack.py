"""build_source_pack: inputs -> one validated `SourcePack`. The only job of Intake.

detect -> extract (the tier ladder) -> structure -> normalise -> chunk -> quality report -> brief hints.
No outline, no resource search, no teaching text (a test checks the imports). Deterministic: the same inputs in the same order give the same ids.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Optional

from .detect import dominant_script, language_of, page_health
from .extract import BasicExtractor, Extractor, FetchFn, IntakeInput, RawPage, RawSource, extract
from .schema import (Brief, Chunk, ChecklistItem, Figure, Goal, LinkFound, OutlineHint, PageInfo, PageQuality, SourceInfo, SourcePack, Table)
from .structure import Block, clean_item, dedupe_pages, links_in, normalize_text, parse_page, strip_furniture

CHUNK_CHARS = 600
CODE_CHARS = 1200
TIER_FOR = {"text": "text", "text-layer": "text-layer", "indra-auto": "indra-auto", "indra-ocr": "indra-ocr", "vision": "vision", "raw-text": "raw-text", "none": "none"}
LEVEL_WORDS = {"beginner": r"\b(beginner|introduct\w+|basics?|from scratch|fundamentals?|101|getting started)\b", "advanced": r"\b(advanced|expert|in depth|deep dive|masterclass)\b"}
TRACKS = (("competitive_programming", r"\b(codeforces|competitive programming|icpc|leetcode|olympiad|dsa)\b"), ("jee", r"\bjee\b|\biit\b"), ("neet", r"\bneet\b"),
          ("boards", r"\b(cbse|icse|board exam|boards|class (?:10|12)|grade (?:10|12)|ncert)\b"))


def _split_long(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for piece in re.split(r"(?<=[.!?])\s+", text):
        if cur and len(cur) + len(piece) + 1 > limit:
            out.append(cur.strip())
            cur = ""
        cur += piece + " "
    if cur.strip():
        out.append(cur.strip())
    return out


class _Builder:
    def __init__(self, sid: str, pack: dict):
        self.sid, self.p = sid, pack
        self.stack: list[tuple[int, str]] = []          # heading path across pages: (level, text)
        self.tcount = self.fcount = 0

    def path(self) -> list[str]:
        return [t for _, t in self.stack]

    def heading(self, level: int, text: str, page: int) -> None:
        while self.stack and self.stack[-1][0] >= level:
            self.stack.pop()
        self.stack.append((level, text))
        self.p["outline_hints"].append(OutlineHint(text=text, level=level, source_id=self.sid, page=page))


def _blocks_to_pack(b: _Builder, blocks: list[Block], page: int, counters: dict, order: list[int]) -> None:
    p = b.p
    cur, cur_kind = "", "prose"

    def put(text: str, kind: str = "prose") -> None:
        counters[page] += 1
        p["chunks"].append(Chunk(id=f"{b.sid}-p{page}-c{counters[page]}", source_id=b.sid, page=page, heading_path=b.path(), text=text.strip(), kind=kind))

    def flush() -> None:
        nonlocal cur
        if cur.strip():
            put(cur)
        cur = ""

    for blk in blocks:
        if blk.type == "heading":
            flush()
            b.heading(max(1, min(6, blk.level or 2)), blk.text, page)
        elif blk.type in ("paragraph", "list"):
            text = blk.text if blk.type == "paragraph" else "\n".join(f"- {i}" for i in blk.items)
            if blk.type == "list":
                flush()
                for piece in _split_long(text, CHUNK_CHARS * 2):
                    put(piece, "list")
                continue
            for piece in _split_long(text, CHUNK_CHARS):
                if cur and len(cur) + len(piece) > CHUNK_CHARS:
                    flush()
                cur += piece + " "
        elif blk.type == "toc":
            flush()
            for item in blk.items:
                order[0] += 1
                p["checklist"].append(ChecklistItem(text=clean_item(item), source_id=b.sid, page=page, order=order[0]))
                p["outline_hints"].append(OutlineHint(text=clean_item(item), level=(b.stack[-1][0] + 1) if b.stack else 2, source_id=b.sid, page=page))
            put("\n".join(f"- {i}" for i in blk.items), "list")
        elif blk.type == "code":
            flush()
            for piece in _split_long(blk.text, CODE_CHARS):
                put(piece, "code")
        elif blk.type == "table":
            flush()
            b.tcount += 1
            tid = f"{b.sid}-t{b.tcount}"
            p["tables"].append(Table(id=tid, source_id=b.sid, page=page, columns=blk.columns, rows=blk.rows, caption=b.stack[-1][1] if b.stack else ""))
            put(f"Table {tid}: columns {', '.join(blk.columns)}; {len(blk.rows)} rows.", "table_ref")
    flush()


def _quality(raw: RawSource, health_of: dict[int, str]) -> tuple[str, list[PageQuality]]:
    """Group the pages by (status, reason, tier) and decide the source status."""
    groups: dict[tuple, list[int]] = defaultdict(list)
    ok = 0
    for i, pg in enumerate(raw.pages, start=1):
        reason = pg.failure or health_of.get(i, "")
        if reason or not pg.text.strip():
            groups[("unreadable", reason or "There is no text on this page.", pg.tier if pg.tier != "none" else (pg.tried[-1] if pg.tried else "none"))].append(i)
        else:
            ok += 1
    report = [PageQuality(pages=v, status=s, reason=r, tier=t if t in ("text", "text-layer", "indra-auto", "indra-ocr", "vision", "raw-text", "none") else "none",
                          tried=sorted({x for i in v for x in raw.pages[i - 1].tried})) for (s, r, t), v in groups.items()]
    report += raw.notes
    status = "unreadable" if ok == 0 else ("readable" if not report else "partly_readable")
    return status, report


def _brief(pack: dict, goal: str, language: str, raw: list[RawSource]) -> Brief:
    corpus = " ".join([goal] + [h.text for h in pack["outline_hints"][:12]] + [c.text for c in pack["chunks"][:6]]).lower()
    level = "advanced" if re.search(LEVEL_WORDS["advanced"], corpus) else "beginner" if re.search(LEVEL_WORDS["beginner"], corpus) else "unknown"
    track = next((t for t, rx in TRACKS if re.search(rx, corpus)), "general")
    pages = sum(len(r.pages) for r in raw)
    heads = [h for h in pack["outline_hints"] if h.level == 1] or pack["outline_hints"]
    subject = (goal.strip() or (raw[0].name if raw and not heads else heads[0].text if heads else ""))[:120]
    syllabus = len(pack["checklist"]) >= 4 or bool(re.search(r"\b(syllabus|curriculum|table of contents)\b", corpus))
    corpus_text = " ".join(c.text for c in pack["chunks"][:20])
    return Brief(subject=subject, language=language or language_of(corpus_text), level_hint=level, track_hint=track,
                 size="small" if pages <= 10 else "medium" if pages <= 60 else "large", looks_like_syllabus=syllabus)


def build_source_pack(inputs: list[IntakeInput], goal: str = "", language: str = "en", *, extractor: Optional[Extractor] = None, fetch: Optional[FetchFn] = None,
                      emitter=None, notebook_id: Optional[str] = None) -> SourcePack:
    """Build the pack. `extractor` supplies the tiers for PDFs and photos (default: text layer + an optional vision callable); `emitter` is an optional
    DecisionEmitter that gets one plain-language record per source."""
    extractor = extractor or BasicExtractor()
    pack: dict = {"sources": [], "pages": [], "outline_hints": [], "chunks": [], "tables": [], "figures": [], "links_found": [], "checklist": [], "markdown": {}}
    raws: list[RawSource] = []
    order = [0]
    for idx, inp in enumerate(inputs, start=1):
        sid = f"s{idx}"
        raw = extract(inp, language, extractor, fetch)
        raws.append(raw)
        texts = [normalize_text(pg.markdown or pg.text) for pg in raw.pages]
        texts, _removed = strip_furniture(texts)
        dup = dedupe_pages(texts)
        health: dict[int, str] = {}
        builder = _Builder(sid, pack)
        counters: dict[int, int] = defaultdict(int)
        md_parts: list[str] = []
        expected = dominant_script(language_hint_text(goal, raw))
        for i, (pg, text) in enumerate(zip(raw.pages, texts), start=1):
            ocr = pg.tier in ("indra-auto", "indra-ocr", "vision")
            if pg.text.strip():
                h = page_health(pg.text, pg.confidence, from_ocr=ocr, expected_script=expected if ocr else None)    # judged on plain text: Markdown/HTML markup is not 'stray characters'
                if not h.ok:
                    health[i] = h.reason
            pack["pages"].append(PageInfo(source_id=sid, page=i, tier=TIER_FOR.get(pg.tier, "none"), source_kind=pg.source_kind, confidence=pg.confidence,
                                          escalated_because=pg.escalated_because))
            if i in health or not text.strip():
                continue
            if dup[i - 1] is not None:
                continue                                                    # an exact repeat of an earlier page: kept once
            blocks = parse_page(text, markdown=bool(pg.markdown))
            _blocks_to_pack(builder, blocks, i, counters, order)
            md_parts.append(text)
            for url, anchor in links_in(pg.text):
                pack["links_found"].append(LinkFound(url=url, anchor_text=anchor, source_id=sid, page=i))
            if raw.kind == "image":
                builder.fcount += 1
                pack["figures"].append(Figure(id=f"{sid}-f{builder.fcount}", source_id=sid, page=i, caption=raw.name, ocr_text=pg.text[:2000], image_ref=raw.name))
        status, report = _quality(raw, health)
        info = SourceInfo(id=sid, kind=raw.kind if raw.kind in ("text", "markdown", "pdf", "image", "url") else "text", name=raw.name or inp.name,
                          language=raw.language or language, pages=raw.total_pages or len(raw.pages), status=status, quality_report=report)
        pack["sources"].append(info)
        pack["markdown"][sid] = "\n\n".join(md_parts)
        if emitter is not None and notebook_id:
            read = sum(1 for i, pg in enumerate(raw.pages, start=1) if pg.text.strip() and i not in health)
            emitter.emit(notebook_id, "intake", "accepted" if status != "unreadable" else "rejected", what=f"{read} of {info.pages} pages of {info.name[:40]}",
                         outcome={"readable": "ok", "partly_readable": "partial", "unreadable": "failed"}[status], counts={"read": read, "pages": info.pages},
                         severity="info" if status == "readable" else "notice", correlation_id=f"intake-{notebook_id}", dedupe_key=f"intake|{notebook_id}|{sid}")
    pack["goal"] = Goal(text=goal, language=language)
    pack["brief"] = _brief(pack, goal, language, raws)
    return SourcePack(**pack)


def language_hint_text(goal: str, raw: RawSource) -> str:
    """Text used to decide which script the learner's material is in (goal sentence plus any text-layer pages)."""
    return goal + "\n" + "\n".join(pg.text for pg in raw.pages if pg.tier in ("text", "text-layer"))[:4000]
