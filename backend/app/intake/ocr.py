"""The OCR tiers of the Intake ladder: Indra OCR in a child process (tiers A and B), then the vision model (tier C) for what Indra cannot be trusted with.

Per page, cheapest first:  A  Indra `auto` (a digital PDF's own text plus structure, OCR only where the text is missing)
                           B  Indra `ocr` for scans and photos in supported scripts
                           C  vision model, only when escalated: low confidence, a script the offline reader cannot read, stray characters, a scan Indra read as empty
The page-health gate decides every escalation and records WHY (`escalated_because`), so nothing is trusted or dropped silently.

`Local OCR only` (privacy): no page image ever leaves the server. Pages that would need tier C are reported as unreadable with that reason.
Without Indra installed (a Windows dev machine) the ladder degrades to the BasicExtractor tiers; the same pack comes out, with fewer structure hints.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .detect import ocr_supports, page_health
from .extract import PACK_MAX_PAGES, BasicExtractor, RawPage, RawSource, VisionFn, classify_empty, pdf_page_facts, render_png
from .detect import language_of
from .schema import PageQuality

LANG_SCRIPT = {"en": "Latin", "hi": "Devanagari", "mr": "Devanagari", "bn": "Bengali", "ta": "Tamil", "te": "Telugu", "kn": "Kannada", "ml": "Malayalam", "gu": "Gujarati", "pa": "Gurmukhi"}
PRIVACY_NOTICE = ("Some pages may be sent to a model provider to be read when they cannot be read on the server. "
                  "Turn on 'Local OCR only' to keep every page on the server; pages that need the provider will then be reported as unreadable.")
LOCAL_ONLY_REASON = "This page needs a stronger reader, which is switched off by 'Local OCR only'."
_SEM = threading.BoundedSemaphore(max(1, int(os.environ.get("AASAAN_OCR_WORKERS", "1"))))     # at most this many OCR processes at once
BACKEND_DIR = Path(__file__).resolve().parents[2]


class OcrError(RuntimeError):
    pass


def local_only_default() -> bool:
    return os.environ.get("AASAAN_LOCAL_OCR_ONLY", "").strip().lower() in ("1", "true", "on", "yes")


def indra_available() -> bool:
    """Indra is installed and its models are on disk (INDRA_OCR_MODEL_DIR), and AASAAN_INDRA is not 'off'."""
    if os.environ.get("AASAAN_INDRA", "on").strip().lower() in ("off", "0", "false", "no"):
        return False
    import importlib.util
    if importlib.util.find_spec("indra_ocr") is None:
        return False
    d = os.environ.get("INDRA_OCR_MODEL_DIR")
    return bool(d) and all((Path(d) / f).exists() for f in ("det.onnx", "rec.onnx", "meta.json"))


@dataclass
class IndraRunner:
    """Runs the worker in a child process with a timeout and a page cap. Returns the parsed JSON, or raises OcrError with a plain reason."""
    cores: int = int(os.environ.get("AASAAN_OCR_CORES", "2"))
    timeout_s: float = float(os.environ.get("AASAAN_OCR_TIMEOUT_S", "300"))
    max_pages: int = PACK_MAX_PAGES

    def run(self, data: bytes, suffix: str, mode: str) -> dict:
        with tempfile.TemporaryDirectory(prefix="aasaan-ocr-") as tmp:
            f = Path(tmp) / f"input{suffix}"
            f.write_bytes(data)
            cmd = [sys.executable, "-m", "app.intake.indra_worker", mode, str(f), str(self.cores), str(self.max_pages)]
            env = {**os.environ, "PYTHONPATH": str(BACKEND_DIR) + os.pathsep + os.environ.get("PYTHONPATH", "")}
            with _SEM:
                try:
                    done = subprocess.run(cmd, capture_output=True, timeout=self.timeout_s, cwd=str(BACKEND_DIR), env=env)
                except subprocess.TimeoutExpired:
                    raise OcrError(f"the reader took longer than {int(self.timeout_s)} seconds") from None
            if done.returncode != 0:
                tail = (done.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or ["unknown error"]
                raise OcrError(f"the reader stopped with an error ({tail[0][:120]})")
            try:
                return json.loads(done.stdout.decode("utf-8"))
            except ValueError as e:
                raise OcrError("the reader returned something unreadable") from e


class LadderExtractor:
    """Intake's real extractor: Indra tiers first, the vision tier only when the gate asks for it, BasicExtractor behaviour when Indra is absent."""

    def __init__(self, vision: Optional[VisionFn] = None, *, local_only: Optional[bool] = None, runner: Optional[IndraRunner] = None,
                 use_indra: Optional[bool] = None, max_pages: int = PACK_MAX_PAGES, vision_pages: int = 12):
        self.vision, self.max_pages, self.vision_pages = vision, max_pages, vision_pages
        self.local_only = local_only_default() if local_only is None else local_only
        self.runner = runner or IndraRunner(max_pages=max_pages)
        self.use_indra = indra_available() if use_indra is None else use_indra
        self.basic = BasicExtractor(None if self.local_only else vision, max_pages=max_pages, vision_pages=vision_pages)

    # -------------------------------------------------------------------------------------------- shared
    def _escalate(self, pg: RawPage, png: Optional[bytes], language: str, label: str, why: str, budget: list[int]) -> RawPage:
        """The cheap tiers did not give trustworthy text for this page: ask the vision tier, unless that is forbidden or unavailable."""
        pg.escalated_because = why
        pg.tried.append("indra")
        if self.local_only:
            pg.text, pg.markdown, pg.tier, pg.failure = "", None, "none", LOCAL_ONLY_REASON
            return pg
        if not self.vision or png is None or budget[0] <= 0:
            pg.text, pg.markdown, pg.tier, pg.failure = "", None, "none", why + (" No stronger reader was available." if not self.vision else "")
            return pg
        budget[0] -= 1
        pg.tried.append("vision")
        r = self.basic._see(png, language, label)
        if r.get("text") and not r.get("failed"):
            v = page_health(r["text"], None, from_ocr=True)
            if v.ok:
                pg.text, pg.markdown, pg.tier, pg.confidence, pg.failure = r["text"], None, "vision", None, ""
                return pg
        pg.text, pg.markdown, pg.tier = "", None, "none"
        pg.failure = "The page is empty." if r.get("empty") else (why + " The stronger reader could not make it out either.")
        return pg

    def _to_page(self, d: dict, expected: str) -> RawPage:
        src = d.get("source", "ocr")
        text = (d.get("markdown") or d.get("text") or "").strip()
        pg = RawPage(text=d.get("text") or "", markdown=d.get("markdown") or None, confidence=d.get("conf_mean"),
                     tier="indra-auto" if src == "text" else "indra-ocr", source_kind="text layer" if src == "text" else "scan or photo", tried=["indra"])
        if src == "error":
            pg.text, pg.markdown, pg.tier, pg.failure = "", None, "none", "The reader could not process this page (" + (d.get("note") or "error")[:80] + ")."
        elif not text:
            pg.tier = "none"
        return pg

    def _gate(self, pg: RawPage, expected: str) -> str:
        """'' when the page is trustworthy, else the plain reason it must be escalated."""
        if pg.tier == "indra-ocr" and pg.text.strip():
            h = page_health(pg.text, pg.confidence, from_ocr=True, expected_script=expected if not ocr_supports(expected) else None)
            return "" if h.ok else h.reason
        return ""

    # -------------------------------------------------------------------------------------------- PDF
    def pdf(self, data: bytes, name: str, language: str) -> RawSource:
        if not self.use_indra:
            return self.basic.pdf(data, name, language)
        try:
            out = self.runner.run(data, ".pdf", "auto")
        except OcrError as e:
            src = self.basic.pdf(data, name, language)
            src.notes.append(PageQuality(pages=[], status="partly_readable", reason=f"The offline reader failed ({e}); the text layer and the stronger reader were used instead.",
                                         tier="text-layer", tried=["indra"]))
            return src
        expected = LANG_SCRIPT.get(language, "Latin")
        facts: dict[int, dict] = {}
        pages: list[RawPage] = []
        budget = [self.vision_pages]
        try:
            _t, empties, total = pdf_page_facts(data, self.max_pages)
            facts = {e["page"]: e for e in empties}
        except Exception:  # noqa: BLE001
            total = len(out["pages"])
        for d in out["pages"]:
            n = d["page"]
            pg = self._to_page(d, expected)
            if pg.tier == "none" and not pg.failure:
                kind = classify_empty(facts[n]) if n in facts else "blank"
                pg.source_kind = kind
                if kind == "scan":
                    pg = self._escalate(pg, render_png(data, n), language, f"{name} p{n}", "Indra read nothing on this scan.", budget)
                else:
                    pg.failure = {"drawing": "The page has only lines or shapes (for example an empty table grid) and no text.", "blank": "The page is blank."}[kind]
            else:
                why = self._gate(pg, expected)
                if why:
                    pg = self._escalate(pg, render_png(data, n), language, f"{name} p{n}", why, budget)
            pages.append(pg)
        src = RawSource("pdf", name, pages, language_of("\n".join(p.text for p in pages if p.tier == "indra-auto")), total_pages=max(total, len(pages)))
        if src.total_pages > len(pages):
            src.notes.append(PageQuality(pages=list(range(len(pages) + 1, src.total_pages + 1)), status="partly_readable",
                                         reason=f"Aasaan reads the first {len(pages)} pages of a file. The rest were not read.", tier="none"))
        return src

    # -------------------------------------------------------------------------------------------- photos and image files
    def image(self, data: bytes, name: str, language: str) -> RawSource:
        if not self.use_indra:
            return self.basic.image(data, name, language)
        suffix = ".png" if data[:4] == b"\x89PNG" else ".jpg"
        try:
            out = self.runner.run(data, suffix, "ocr")
        except OcrError as e:
            src = self.basic.image(data, name, language)
            src.notes.append(PageQuality(pages=[], status="partly_readable", reason=f"The offline reader failed ({e}).", tier="vision", tried=["indra"]))
            return src
        expected = LANG_SCRIPT.get(language, "Latin")
        pg = self._to_page(out["pages"][0], expected) if out["pages"] else RawPage(tier="none", failure="The photo has no readable text.")
        pg.source_kind = "photo"
        why = self._gate(pg, expected) if pg.text.strip() else "Indra read nothing in this photo."
        if why:
            pg = self._escalate(pg, data, language, name, why, [self.vision_pages])
            pg.source_kind = "photo"
        return RawSource("image", name, [pg], language_of(pg.text), total_pages=1)


def default_extractor(vision: Optional[VisionFn] = None, local_only: Optional[bool] = None) -> LadderExtractor:
    return LadderExtractor(vision, local_only=local_only)
