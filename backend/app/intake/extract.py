"""Extraction: turn one input into raw pages (text, optional Markdown, tier, confidence). The ladder, cheapest first:

    text   pasted text / Markdown / a fetched link
    text-layer   a PDF's own text (pypdf)
    (Indra OCR tiers and the vision tier plug in through the `Extractor` protocol; see ocr.py)

Everything here is plain code. The only model call is the injected `vision` callable (used by `BasicExtractor` for pages with no text and for photos).
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from .detect import MIN_CHARS, language_of, page_health
from .schema import PageQuality

PACK_MAX_PAGES = 120          # the old path read 20; the pack reads more and says so when it stops
PAGE_CHARS = 1800
VECTOR_TEXT_BYTES = 20_000


@dataclass
class RawPage:
    text: str = ""
    markdown: Optional[str] = None
    tier: str = "none"                    # text | text-layer | indra-auto | indra-ocr | vision | raw-text | none
    source_kind: str = ""                 # text layer | scan | photo | drawing | blank
    confidence: Optional[float] = None
    escalated_because: str = ""
    failure: str = ""                     # plain reason when the page could not be read at all
    tried: list[str] = field(default_factory=list)


@dataclass
class RawSource:
    kind: str                             # text | markdown | pdf | image | url
    name: str
    pages: list[RawPage]
    language: str = ""
    total_pages: int = 0                  # pages in the file (may exceed what was read)
    notes: list[PageQuality] = field(default_factory=list)   # extra quality facts (for example "stopped at the page limit")
    url: str = ""


@dataclass
class IntakeInput:
    kind: str                             # text | markdown | pdf | image | url
    name: str
    text: str = ""
    data: bytes = b""
    url: str = ""


VisionFn = Callable[[bytes, str, str], dict]      # (png bytes, language, label) -> {"text", "empty", "failed"}
FetchFn = Callable[[str], dict]                   # url -> {"ok": bool, "title", "text", "error"}


class Extractor(Protocol):
    def pdf(self, data: bytes, name: str, language: str) -> RawSource: ...
    def image(self, data: bytes, name: str, language: str) -> RawSource: ...


# ------------------------------------------------------------------------------------------------ text
def paginate_text(text: str) -> list[str]:
    """Pasted text -> pseudo pages. A form feed splits pages; otherwise about PAGE_CHARS on paragraph breaks. No page cap here."""
    text = text.replace("\r\n", "\n").strip()
    if "\f" in text:
        return [p.strip() for p in text.split("\f") if p.strip()]
    pages, cur = [], ""
    for para in re.split(r"\n\s*\n", text):
        if cur and len(cur) + len(para) > PAGE_CHARS:
            pages.append(cur.strip())
            cur = ""
        cur += para + "\n\n"
    if cur.strip():
        pages.append(cur.strip())
    return pages


def from_text(inp: IntakeInput) -> RawSource:
    md = inp.kind == "markdown"
    pages = [RawPage(text=p, markdown=p if md else None, tier="text", source_kind="text") for p in paginate_text(inp.text or "")]
    return RawSource(inp.kind, inp.name, pages, language_of(inp.text or ""), total_pages=len(pages))


def from_url(inp: IntakeInput, fetch: Optional[FetchFn]) -> RawSource:
    if fetch is None:
        return RawSource("url", inp.name or inp.url, [], url=inp.url, notes=[PageQuality(pages=[], status="unreadable", reason="Links cannot be read in this setup.")])
    got = fetch(inp.url)
    if not got.get("ok"):
        return RawSource("url", inp.name or inp.url, [], url=inp.url,
                         notes=[PageQuality(pages=[], status="unreadable", reason=str(got.get("error") or "The page could not be opened."), tried=["fetch"])])
    pages = [RawPage(text=p, tier="text", source_kind="text") for p in paginate_text(got.get("text", ""))]
    return RawSource("url", got.get("title") or inp.name or inp.url, pages, language_of(got.get("text", "")), total_pages=len(pages), url=inp.url)


# ------------------------------------------------------------------------------------------------ PDF text layer
def pdf_page_facts(data: bytes, max_pages: int = PACK_MAX_PAGES) -> tuple[list[str], list[dict], int]:
    """(page texts, facts for pages with no text, total pages in the file). Facts say WHY a page has no text: scan, drawing only, or blank."""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    total = len(reader.pages)
    texts, empties = [], []
    for n, p in enumerate(reader.pages[:max_pages], start=1):
        t = (p.extract_text() or "").strip()
        texts.append(t)
        if t:
            continue
        try:
            images = len(list(p.images))
        except Exception:  # noqa: BLE001  odd image objects must not stop reading
            images = 0
        res = p.get("/Resources") or {}
        fonts = len((res.get("/Font") or {}) if hasattr(res, "get") else {})
        size = 0
        try:
            c = p.get("/Contents")
            obj = c.get_object() if c is not None else None
            parts = obj if isinstance(obj, list) else [obj] if obj is not None else []
            size = sum(len(x.get_object().get_data()) for x in parts)
        except Exception:  # noqa: BLE001
            pass
        empties.append({"page": n, "images": images, "fonts": fonts, "content_bytes": size})
    return texts, empties, total


def render_png(data: bytes, page: int, scale: float = 1.6) -> Optional[bytes]:
    try:
        import pypdfium2 as pdfium
        buf = io.BytesIO()
        pdfium.PdfDocument(data)[page - 1].render(scale=scale).to_pil().convert("RGB").save(buf, "PNG")
        return buf.getvalue()
    except Exception:  # noqa: BLE001  no renderer, or a damaged page: the caller reports "could not read", never crashes
        return None


def classify_empty(fact: dict) -> str:
    if fact["images"]:
        return "scan"
    if fact["content_bytes"] >= VECTOR_TEXT_BYTES and not fact["fonts"]:
        return "drawing"
    return "blank"


class BasicExtractor:
    """The tiers that need no OCR engine: text layer, then the vision model for scans and photos (when a vision callable is given)."""

    def __init__(self, vision: Optional[VisionFn] = None, max_pages: int = PACK_MAX_PAGES, vision_pages: int = 12):
        self.vision, self.max_pages, self.vision_pages = vision, max_pages, vision_pages

    def _see(self, png: bytes, language: str, label: str) -> dict:
        """One vision call. A model that errors or is switched off means "could not read", never an exception into the pack."""
        try:
            return self.vision(png, language, label) or {"failed": True}
        except Exception:  # noqa: BLE001
            return {"failed": True}

    def pdf(self, data: bytes, name: str, language: str) -> RawSource:
        try:
            texts, empties, total = pdf_page_facts(data, self.max_pages)
        except Exception as e:  # noqa: BLE001
            return RawSource("pdf", name, [], notes=[PageQuality(pages=[], status="unreadable", reason=f"The file could not be opened ({type(e).__name__}).", tried=["text-layer"])])
        by = {e["page"]: e for e in empties}
        pages: list[RawPage] = []
        used = 0
        for n, t in enumerate(texts, start=1):
            if t:
                pages.append(RawPage(text=t, tier="text-layer", source_kind="text layer"))
                continue
            kind = classify_empty(by[n])
            pg = RawPage(tier="none", source_kind=kind, tried=["text-layer"])
            if kind == "scan" and self.vision and used < self.vision_pages:
                used += 1
                png = render_png(data, n)
                r = self._see(png, language, f"{name} p{n}") if png else {"failed": True}
                pg.tried.append("vision")
                if r.get("text") and not r.get("failed"):
                    pg.text, pg.tier = r["text"], "vision"
                    pg.escalated_because = "the page is a scan with no text layer"
                elif r.get("empty"):
                    pg.failure = "The page is a scan with no text on it."
                else:
                    pg.failure = "The page is a scan and the reader could not make out its text."
            elif kind == "scan":
                pg.failure = "The page is a scan, but no reader was available for it."
            elif kind == "drawing":
                pg.failure = "The page has only lines or shapes (for example an empty table grid) and no text."
            else:
                pg.failure = "The page is blank."
            pages.append(pg)
        src = RawSource("pdf", name, pages, language_of("\n".join(texts)), total_pages=total)
        if total > len(texts):
            src.notes.append(PageQuality(pages=list(range(len(texts) + 1, total + 1)), status="partly_readable",
                                         reason=f"Aasaan reads the first {len(texts)} pages of a file. The rest were not read.", tier="none"))
        return src

    def image(self, data: bytes, name: str, language: str) -> RawSource:
        if not self.vision:
            return RawSource("image", name, [RawPage(tier="none", source_kind="photo", failure="No reader was available for photos.")], total_pages=1)
        r = self._see(data, language, name)
        pg = RawPage(tier="none", source_kind="photo", tried=["vision"])
        if r.get("text") and not r.get("failed"):
            pg.text, pg.tier = r["text"], "vision"
        else:
            pg.failure = "The photo has no readable text." if r.get("empty") else "The reader could not make out the text in this photo."
        return RawSource("image", name, [pg], language_of(pg.text), total_pages=1)


def extract(inp: IntakeInput, language: str, extractor: Extractor, fetch: Optional[FetchFn] = None) -> RawSource:
    if inp.kind in ("text", "markdown"):
        return from_text(inp)
    if inp.kind == "url":
        return from_url(inp, fetch)
    if inp.kind == "pdf":
        return extractor.pdf(inp.data, inp.name, language)
    if inp.kind == "image":
        return extractor.image(inp.data, inp.name, language)
    return RawSource(inp.kind, inp.name, [], notes=[PageQuality(pages=[], status="unreadable", reason=f"Files of kind '{inp.kind}' are not supported yet.")])


def healthy(pg: RawPage) -> bool:
    return bool(pg.text.strip()) and page_health(pg.text, pg.confidence, from_ocr=pg.tier in ("indra-ocr", "indra-auto", "vision")).ok and len(pg.text.strip()) >= MIN_CHARS
