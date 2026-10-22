"""Aasaan engine: notebooks built progressively by a swarm of agents.

Phase 0  outline      Intake reads the inputs, Architect plans 8-20 chapters (roadmap + graph).
Phase 1  chapter 1    Curator -> Teacher <-> Critic (max 2 revisions) -> Quizzer; the notebook opens when it is ready.
Phase 2  background   the remaining chapters are built by the worker (jobs table), student-priority first.

State lives in SQLite, so everything is resumable after a restart. The UI never sees agent names.
"""
from __future__ import annotations

import base64
import contextvars
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
import re
import time
import uuid
from pathlib import Path
from typing import Optional

from . import web
from .agents import architect, companion, critic, curator, diagnoser, doubt, gates, illustrator, intake, practice as practice_agent, quizzer, rewriter, slides as slides_agent, teacher
from .agents.common import overlap, tokens
from . import coverage, depth as depth_mod, readability, quiz_grading
from .quiz_grading import correct_text
from .orchestrator.decisions import DecisionEmitter
from .orchestrator.referee import orchestrator_mode
from .orchestrator.shadow import Shadow
from .tools import current_notebook
from .db import Store
from . import edits
from .tools import ToolRunner
from .router import AllFailed, ModelRouter
from .schemas import (MAX_INSERTS_PER_SESSION, ChapterContent, CriticOut, IntakeOut, LearnerProfile, OutlineOut,
                      Question, QuizOut, resolve_key)
from .voice import Voice

log = logging.getLogger("aasaan.engine")

MAX_REVISIONS = 2          # Teacher <-> Critic loops before shipping the best draft as unverified
MAX_JOB_ATTEMPTS = 3
MAX_SOURCES = 6
MAX_FILE_BYTES = 15 * 1024 * 1024
MATERIAL_MIN_CHARS = 600   # pasted text longer than this is treated as study material, not as a goal
READINESS_WEIGHT = {"queued": 0.0, "drafting": 0.4, "checking": 0.8, "ready": 1.0, "failed": 0.0}


def friendly_error(e: Exception, notebook: bool = True) -> str:
    """What the student sees when a build fails. The technical detail stays in the job row, the log and `events`."""
    text = str(e)
    if isinstance(e, AllFailed) or "cannot plan" in text or "every step in the chain failed" in text:
        return ("I couldn't reach any AI model to plan your course. Please try again in a moment. "
                "(If this keeps happening, the server's model keys need checking: /api/health/models.)")
    if "couldn't read any of your material" in text:
        return "I couldn't read any of your material. Please paste the text or attach a clearer photo or PDF."
    return "Something went wrong while building this. Please try again."


from .errors import UserError  # noqa: E402,F401  (re-exported: the API and the tests import it from here)


def split_request(text: str) -> tuple[Optional[str], str, list[str]]:
    """The chat box holds a goal, pasted material, links, or a mix. Returns (goal, material_text, urls)."""
    text = (text or "").strip()
    urls = web.extract_urls(text)
    rest = web.URL_RE.sub("", text).strip()
    if len(rest) >= MATERIAL_MIN_CHARS:
        return None, rest, urls
    return (rest or None), "", urls


COVER_IMAGES = ("thumb01.jpg", "thumb02.jpg", "thumb03.jpg", "thumb04.jpg")


def cover_image(nid: str) -> str:
    """One of the four notebook thumbnails in frontend/resources, picked from the notebook id so it never changes."""
    import hashlib
    return COVER_IMAGES[int(hashlib.sha1(nid.encode()).hexdigest(), 16) % len(COVER_IMAGES)]


def search_topic(nb: dict) -> str:
    """Short subject for web searches. A long personal goal ("I'm part of the training for ... i need a roadmap") would poison
    every query, so past a short single line the notebook title (the Architect's name for the subject) is used instead."""
    goal = (nb.get("goal") or "").strip()
    return goal if goal and len(goal) <= 60 and "\n" not in goal else (nb.get("title") or goal)[:60]


class Engine:
    def __init__(self, store: Store, router: ModelRouter, upload_dir: Optional[str | Path] = None,
                 retry_delay_s: float = 4.0, tools: Optional[ToolRunner] = None):
        self.store, self.router = store, router
        self.tools = tools
        if tools is not None:
            web.set_runner(tools)
        web.set_page_gate(lambda url, title, text: gates.page_health(router, url, title, text))   # page_health after each fetch tier
        self.voice = Voice(router)
        if upload_dir is None:
            from .settings import get_uploads_dir
            upload_dir = get_uploads_dir()
        self.decisions = DecisionEmitter(store)
        if getattr(router, "sink", None) is not None:
            router.sink.hook = self._router_event
        self.upload_dir = Path(upload_dir)
        self.retry_delay_s = retry_delay_s

    # ================================================================= orchestrator (shadow mode)
    def _shadow(self, nid: str) -> Optional[Shadow]:
        """The board mirror for this notebook when AASAAN_ORCHESTRATOR is on (or per-notebook and opted in), else None. The old build stays in charge."""
        if orchestrator_mode() == "off":
            return None
        if getattr(self, "_shadow_obj", None) is None:
            self._shadow_obj = Shadow(self.store, self.decisions)
        return self._shadow_obj if self._shadow_obj.enabled(nid) else None

    def _sh(self, nid: str, name: str, *a, **kw):
        """Report a step to the board. A board problem is logged and ignored: observing must never fail a build."""
        try:
            sh = self._shadow(nid)
            return getattr(sh, name)(nid, *a, **kw) if sh else None
        except Exception as e:  # noqa: BLE001
            self.router.note("referee", "fallback", f"board mirror skipped ({name})", f"{type(e).__name__}: {e}"[:150])
            return None

    # ================================================================= activity feed
    def _dec(self, nid: str, agent: str, kind: str, what: str, **kw) -> None:
        """One plain-language decision record for the learner's Activity feed. Never raises: the feed must not break a build."""
        try:
            self.decisions.emit(nid, agent, kind, what=what, **kw)
        except Exception:  # noqa: BLE001
            pass

    _FEED_AGENTS = {"intake": "intake", "architect": "architect", "curator": "curator", "teacher": "teacher", "critic": "critic", "quizzer": "quizzer",
                    "illustrator": "illustrator", "doubt": "doubt", "editor": "editor", "diagnoser": "diagnoser", "rewriter": "rewriter", "scoper": "scoper"}

    def _router_event(self, agent: str, provider: str, status: str, summary: str, error: str) -> None:
        """A model or tool fell back, retried or failed: say so calmly. No model name, no raw error text: only a fixed reason."""
        nid = current_notebook.get()
        if not nid or status not in ("fallback", "retry", "failed"):
            return
        who = self._FEED_AGENTS.get(agent.split(":")[0].split(".")[0], "referee")
        limit = bool(re.search(r"429|rate|quota|too many", error or "", re.I))
        bucket = int(time.time() // 30)                                  # at most one record per agent and status every 30 s
        self._dec(nid, who, "fell_back", "this step", reason_code="rate_limited" if limit else "backup_model", outcome="failed" if status == "failed" else "partial",
                  severity="warning" if status == "failed" else "notice", dedupe_key=f"ev|{nid}|{who}|{status}|{bucket}")

    def decisions_view(self, nid: str, since: int = 0, chapter_id: Optional[int] = None, agent: Optional[str] = None, severity: Optional[str] = None) -> dict:
        self._nb(nid)
        return {"decisions": self.decisions.recent(nid, since_id=since, chapter_id=chapter_id, agent=agent, severity=severity)}

    # ================================================================= helpers
    def profile(self, learner_id: str, nb: Optional[dict] = None) -> LearnerProfile:
        return LearnerProfile(learner_id=learner_id, language=(nb or {}).get("language") or "en",
                              level=(nb or {}).get("level") or "beginner", goal=(nb or {}).get("goal"),
                              mistake_labels=self.store.mistake_labels(learner_id))

    def _nb(self, nid: str) -> dict:
        nb = self.store.get_notebook(nid)
        if not nb:
            raise UserError("unknown notebook")
        return nb

    def _chapter(self, cid: int) -> dict:
        ch = self.store.get_chapter(cid)
        if not ch:
            raise UserError("unknown chapter")
        return ch

    def _all_chunks(self, nid: str, books: bool = False) -> list[dict]:
        """Every chunk of the student's own material. Books are only for the chat to look things up in (books=True), never course input."""
        out = []
        for s in self.store.sources(nid):
            if s["kind"] == "book" and not books:
                continue
            for c in s["chunks"]:
                out.append({**c, "source_id": s["id"]})
        return out

    def _chapter_chunks(self, ch: dict, all_chunks: list[dict], k: int = 6) -> list[dict]:
        """The student's own material most relevant to a chapter: cited chunks first, then keyword overlap."""
        by_id = {c["chunk_id"]: c for c in all_chunks}
        picked = [by_id[r] for r in ch.get("source_refs", []) if r in by_id][:8]
        if len(picked) < 3 and all_chunks:
            words = tokens(ch["title"] + " " + " ".join(ch.get("objectives", [])) + " " + ch.get("summary", ""), 3)
            ranked = sorted((c for c in all_chunks if c not in picked),
                            key=lambda c: -overlap(words, c["text"]))
            for c in ranked:
                if len(picked) >= k:
                    break
                if overlap(words, c["text"]):
                    picked.append(c)
        return picked

    # ============================================================= notebooks
    def create_notebook(self, learner_id: str, text: str, language: str, files: list[tuple[str, str, bytes]]) -> str:
        """Validate the chat-box request, store the inputs, queue phase 0+1. Returns the notebook id immediately."""
        if not self.router.model_ready():
            raise UserError("Aasaan isn't connected to an AI model yet. Add GEMINI_API_KEY to the .env file (see .env.example) and restart the server.")
        goal, material, urls = split_request(text)
        if not (goal or material or urls or files):
            raise UserError("Type a goal, paste notes or links, or attach a photo or PDF.")
        if len(files) > MAX_SOURCES:
            raise UserError(f"Please attach at most {MAX_SOURCES} files.")
        for name, ctype, data in files:
            if len(data) > MAX_FILE_BYTES:
                raise UserError(f"{name} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
            if not (ctype == "application/pdf" or ctype.startswith("image/") or name.lower().endswith(".pdf")):
                raise UserError(f"{name}: only photos and PDFs can be attached.")
        if goal and not (material or urls or files):   # input_gate: only a confident "unsafe" or "not a request" stops a plain goal
            refusal = gates.input_gate(self.router, goal)
            if refusal:
                raise UserError(refusal)
        title = (goal or (urls[0] if urls else None) or (files[0][0] if files else None) or "New notebook")[:60]
        nid = self.store.create_notebook(learner_id, goal, language or "en", title)
        if material:
            self.store.add_source(nid, "text", "pasted", "Pasted notes", material, [])
        for u in urls[:MAX_SOURCES]:
            self.store.add_source(nid, "url", u, u, "", [])
        folder = self.upload_dir / nid
        for name, ctype, data in files:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / re.sub(r"[^\w.\-]", "_", name)[:80]
            path.write_bytes(data)
            is_pdf = ctype == "application/pdf" or name.lower().endswith(".pdf")
            self.store.add_source(nid, "pdf" if is_pdf else "image", str(path), name, "", [])
        self.store.add_job(nid, None, "notebook")
        return nid

    def delete_notebook(self, nid: str) -> None:
        self._nb(nid)
        self.store.delete_notebook(nid)

    # ================================================================== jobs
    def process_job(self, jid: int) -> None:
        """Run one job. Failures are retried with backoff; after MAX_JOB_ATTEMPTS the chapter/notebook is marked failed
        with the reason (never left stuck in drafting)."""
        job = self.store.get_job(jid)
        if not job or job["state"] not in ("queued", "running"):
            return
        attempts = job["attempts"] + 1
        self.store.set_job(jid, "running", attempts=attempts)
        try:
            if job["kind"] == "notebook":
                self._run_notebook_job(job["notebook_id"])
            elif job["kind"] == "chapter":
                self.build_chapter(job["chapter_id"])
            elif job["kind"] == "resource":
                self.process_resource(job["notebook_id"], json.loads(job["payload"] or "{}").get("source_id", ""))
            self.store.set_job(jid, "done")
        except Exception as e:  # any failure is recorded, never swallowed
            msg = f"{type(e).__name__}: {e}"[:500]
            log.warning("[aasaan] job %s (%s) attempt %d failed: %s", jid, job["kind"], attempts, msg)
            if attempts < MAX_JOB_ATTEMPTS:
                self.store.set_job(jid, "queued", msg, attempts=attempts, delay_s=self.retry_delay_s * attempts)
                if job["chapter_id"]:
                    self.store.set_chapter_status(job["chapter_id"], "queued", msg)
            else:
                self.store.set_job(jid, "failed", msg, attempts=attempts)
                if job["kind"] == "resource":
                    sid = json.loads(job["payload"] or "{}").get("source_id", "")
                    self.store.set_source_meta(sid, {"status": "unreadable", "issues": [{"kind": "failed", "pages": [], "checked_with_vision": False,
                                                                                         "reason": friendly_error(e, notebook=False)}]})
                    self.say(job["notebook_id"], "I couldn't read that file. You can try adding it again.", key=f"readfail:{sid}")
                if job["notebook_id"] and job["kind"] == "notebook":
                    self.say(job["notebook_id"], friendly_error(e), key=f"failed:{attempts}")
                if job["chapter_id"]:
                    self.store.set_chapter_status(job["chapter_id"], "failed", friendly_error(e, notebook=False))
                if job["kind"] == "notebook":
                    chs = self.store.chapters(job["notebook_id"])
                    first = chs[0] if chs else None
                    if first:
                        self.store.set_chapter_status(first["id"], "failed", friendly_error(e))
                    self.store.update_notebook(job["notebook_id"], status="failed", error=friendly_error(e))
        self._refresh_status(job["notebook_id"])

    def _run_notebook_job(self, nid: str) -> None:
        nb = self._nb(nid)
        if not self.store.chapters(nid):
            self.store.update_notebook(nid, status="outlining", error="")
            self.phase0(nid)
        chapters = self.store.chapters(nid)
        first = chapters[0]
        self.store.update_notebook(nid, status="drafting_first", error="")
        if first["status"] != "ready":
            self.build_chapter(first["id"])
        # phase 2: queue the rest (idempotent: skip chapters that already have a live job or are ready)
        for ch in self.store.chapters(nid)[1:]:
            j = self.store.job_for_chapter(ch["id"])
            if ch["status"] != "ready" and not (j and j["state"] in ("queued", "running")):
                self.store.add_job(nid, ch["id"], "chapter")
        self._refresh_status(nid)

    # ============================================================== phase 0
    def _scope(self, nid: str):
        """Tool calls made inside count against this notebook's per-tool budgets (e.g. Firecrawl: 20)."""
        import contextlib
        return self.tools.notebook_scope(nid) if self.tools else contextlib.nullcontext()

    def phase0(self, nid: str) -> None:
        with self._scope(nid):
            return self._phase0(nid)

    def _phase0(self, nid: str) -> None:
        nb = self._nb(nid)
        prof = self.profile(nb["learner_id"], nb)
        warnings: list[str] = []
        topics: list[dict] = []
        srcs = [s for s in self.store.sources(nid) if s["kind"] != "book"][:MAX_SOURCES]     # books are for the chat to look up, not course input
        if not srcs:
            self.say(nid, "Planning your course from your goal.", key="planning")
        for src in srcs:
            label = {"pdf": "your PDF", "url": "your link", "text": "your notes", "image": "your photo"}.get(src["kind"], "your file")
            self.say(nid, f"Reading {label}.", key=f"reading:{src['id']}", refs=[{"kind": "resource", "id": src["id"], "label": src["title"][:50]}])
            try:
                got = self._read_source(src, nb, warnings)
            except AllFailed:
                warnings.append(f"Could not read {src['title'][:60]}")
                continue
            if not got:
                continue
            res: IntakeOut = got
            self.store.update_source_chunks(src["id"], [c.model_dump() for c in res.chunks])
            fresh = next((s for s in self.store.sources(nid) if s["id"] == src["id"]), src)
            self._say_source_read(nid, src, fresh.get("meta") or {"status": "ready"}, len(res.chunks))
            for t in res.topics:
                topics.append({**t.model_dump(), "source_id": src["id"]})
            warnings += [w for w in res.warnings if not re.match(r"page \d+ unreadable or empty$", w)]   # shown once per file in Resources instead
        chunks = self._all_chunks(nid)
        self.store.cache_set(f"nbwarn:{nid}", warnings)
        if not (nb["goal"] or topics):
            raise ValueError("I couldn't read any of your material. Please paste the text or attach a clearer file.")
        scope = gates.scoper(self.router, nb["goal"] or "", topics)
        if scope:
            self.router.note("scoper", "ok", f"scoped as {scope.get('track', '?')} / {scope.get('level', '?')}", f"decided by {scope['by']}")
            self.store.cache_set(f"scope:{nid}", {k: scope.get(k) for k in ("track", "level")})
            if scope.get("level") and not nb.get("level"):
                prof.level = scope["level"]
        res = architect.run(self.router, nb["goal"], topics, chunks, prof, nb["title"], (scope or {}).get("track"))
        out: OutlineOut = res.value
        key_to_id: dict[str, int] = {}
        for i, c in enumerate(out.chapters, start=1):
            key_to_id[c.key] = self.store.add_chapter(nid, i, c.title, c.summary, c.objectives, [], c.source_refs,
                                                      priority=100 if i == 1 else 0)
        for c in out.chapters:
            self.store.set_chapter_prereqs(key_to_id[c.key], [key_to_id[p] for p in c.prereqs])
        self.store.update_notebook(nid, title=out.title[:80], description=out.description, level=out.level,
                                   goal=nb["goal"] or out.title)
        self.say(nid, f"The outline is ready: {len(out.chapters)} chapters. I'm writing chapter 1 first.", key="outline")
        self._dec(nid, "architect", "planned", f"{len(out.chapters)} chapters", counts={"chapters": len(out.chapters)}, dedupe_key=f"outline|{nid}")
        self._sh(nid, "outline", len(out.chapters))
        self._check_coverage(nid, chunks)
        self._source_books(nid, self._nb(nid))

    def _read_source(self, src: dict, nb: dict, warnings: list[str]) -> Optional[IntakeOut]:
        """Turn one source into chunks and topics. Text/PDF/URL chunking is code; photos go to the vision model."""
        kind, sid, lang = src["kind"], src["id"], nb["language"]
        pages: list[str] = []
        images = None
        if kind == "text":
            pages = intake.pages_from_text(src["extracted_text"])
        elif kind == "url":
            page = web.fetch_page(src["raw_ref"], self.store)
            if not page.ok:
                warnings.append(page.error)
                return None
            self.store.set_source_text(src["id"], page.title or src["title"], page.text)
            pages = intake.pages_from_text(page.text)
        elif kind == "pdf":
            try:
                data = Path(src["raw_ref"]).read_bytes()
                pages, empties = intake.analyze_pdf(data)
            except Exception as e:
                warnings.append(f"Could not read PDF {src['title']}: {e}")
                self.store.set_source_meta(sid, {"status": "unreadable", "issues": [{"kind": "unreadable", "pages": [], "reason": str(e)[:200]}]})
                return None
            pages, meta = self._recover_pages(data, pages, empties, lang, src["title"])
            in_file = intake.pdf_page_count(data)
            if in_file > len(pages):   # intake reads a bounded number of pages: say so instead of silently ignoring the rest
                meta["pages_total"] = in_file
                meta["issues"].append({"kind": "over_limit", "pages": list(range(len(pages) + 1, in_file + 1)), "checked_with_vision": False,
                                       "reason": f"Aasaan reads the first {len(pages)} pages of a file. The rest were not read."})
                meta["status"] = "partly_readable" if meta["status"] == "ready" else meta["status"]
            self.store.set_source_meta(sid, meta)
            if not any(p.strip() for p in pages):
                warnings.append(f"{src['title']} has no readable text. Attach clearer photos of the pages instead.")
                return None
        elif kind == "image":
            ext = Path(src["raw_ref"]).suffix.lower().lstrip(".")
            mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}.get(ext, "image/jpeg")
            images = [(mime, Path(src["raw_ref"]).read_bytes())]
        res = intake.run(self.router, sid, pages, images, lang, nb["goal"])
        return res.value

    def _recover_pages(self, data: bytes, pages: list[str], empties: list[dict], language: str, title: str) -> tuple[list[str], dict]:
        """Pages with no text layer: scans (images) and pages that might hold outlined text go to a vision model; drawing-only pages are probed
        once per kind and, if the probe finds no text, reported as such without spending a call on each. Returns (pages, source meta)."""
        pages = list(pages)
        total, recovered = len(pages), []
        todo, probe_groups = [], {}
        for e in empties:
            if e["images"] > 0 or (e["fonts"] == 0 and e["content_bytes"] >= intake.VECTOR_TEXT_BYTES):
                todo.append(e)
            elif e["content_bytes"] > 0:
                probe_groups.setdefault(e["content_bytes"] // 1000, []).append(e)
        no_text: dict[str, list[int]] = {"blank": [e["page"] for e in empties if e["content_bytes"] == 0 and e["images"] == 0], "drawing_only": [], "failed": []}
        vision_checked = False
        budget = intake.OCR_MAX_PAGES
        for group in list(probe_groups.values())[:2]:          # one vision probe per kind of drawing-only page
            png = intake.render_page_png(data, group[0]["page"])
            if png is None or budget <= 0:
                no_text["drawing_only"] += [e["page"] for e in group]
                continue
            budget -= 1
            got = intake.ocr_page(self.router, png, language, f"{title[:30]} p{group[0]['page']}")
            vision_checked = True
            if got["failed"]:
                no_text["failed"] += [e["page"] for e in group]
            elif got["empty"]:
                no_text["drawing_only"] += [e["page"] for e in group]
            else:                                              # the probe found text: this kind of page is worth reading in full
                pages[group[0]["page"] - 1] = got["text"]
                recovered.append(group[0]["page"])
                todo += group[1:]
        for g in list(probe_groups.values())[2:]:              # more than two kinds of drawing-only page: not worth more probes
            no_text["drawing_only"] += [e["page"] for e in g]
        for e in todo:
            if budget <= 0:
                no_text["failed"].append(e["page"])
                continue
            png = intake.render_page_png(data, e["page"])
            if png is None:
                no_text["failed"].append(e["page"])
                continue
            budget -= 1
            got = intake.ocr_page(self.router, png, language, f"{title[:30]} p{e['page']}")
            vision_checked = True
            if got["failed"]:
                no_text["failed"].append(e["page"])
            elif got["empty"]:
                no_text["drawing_only" if e["images"] == 0 else "blank"].append(e["page"])
            else:
                pages[e["page"] - 1] = got["text"]
                recovered.append(e["page"])
        reasons = {"blank": "Nothing is printed on these pages.",
                   "drawing_only": "These pages have only lines or shapes, for example an empty table grid, and no text" +
                                   (" (checked with a vision model)." if vision_checked else "."),
                   "failed": "These pages may hold text, but it could not be read automatically. A clearer scan or photo would help."}
        issues = [{"kind": k, "pages": sorted(set(v)), "reason": reasons[k], "checked_with_vision": vision_checked and k != "failed"} for k, v in no_text.items() if v]
        read = sum(1 for p in pages if p.strip())
        status = "unreadable" if read == 0 else ("partly_readable" if issues else "ready")
        if recovered:
            self.router.note("intake", "ok", f"read {len(recovered)} page(s) of '{title[:40]}' with a vision model", f"pages {recovered[:12]}: no text layer")
        for i in issues:
            log.info("[aasaan] %s: %d page(s) with no text (%s): %s", title, len(i["pages"]), i["kind"], i["reason"])
        return pages, {"status": status, "pages_total": total, "pages_read": read, "recovered_with_vision": sorted(recovered), "issues": issues}

    # =============================================================== chat thread
    def say(self, nid: str, text: str, key: Optional[str] = None, refs: Optional[list] = None, kind: str = "system") -> Optional[int]:
        """Progress narration in the chat: fixed wording written by code from real state, never by a model, so it cannot contradict the course.
        A key makes it idempotent (a retried job does not repeat itself)."""
        try:
            return self.store.add_message(nid, "assistant", text, kind=kind, refs=refs, key=key)
        except Exception as e:  # noqa: BLE001  narration must never break a build
            log.warning("[aasaan] could not post chat message: %s", e)
            return None

    @staticmethod
    def _chapter_ref(ch: dict) -> dict:
        return {"kind": "chapter", "id": ch["id"], "label": ch["title"][:60]}

    def _announce_chapter(self, ch: dict, verified: bool) -> None:
        pos = ch["position"]
        text = f"Chapter {pos} is ready: {ch['title']}." + (" You can start reading while I write the rest." if pos == 1 else "")
        self.say(ch["notebook_id"], text, key=f"ready:{ch['id']}", refs=[self._chapter_ref(ch)])
        if not verified:
            self.say(ch["notebook_id"], f"A few claims in chapter {pos} couldn't be fully verified. The chapter page lists them, and you can re-check.",
                     key=f"unverified:{ch['id']}", refs=[self._chapter_ref(ch)])

    def _say_source_read(self, nid: str, src: dict, meta: dict, n_chunks: int) -> None:
        label = {"pdf": "your PDF", "url": "your link", "text": "your notes", "image": "your photo"}.get(src["kind"], "your file")
        status = (meta or {}).get("status", "ready")
        if status == "unreadable":
            hint = "The site may need a login or block automated readers. You can paste the text instead." if src["kind"] == "url" else "A clearer copy would help."
            self.say(nid, f"I couldn't read {label} \"{src['title'][:50]}\". {hint}", key=f"read:{src['id']}", refs=[{"kind": "resource", "id": src["id"], "label": src["title"][:50]}])
            return
        text = f"Finished reading {label} \"{src['title'][:50]}\"" + (f" ({meta['pages_read']} of {meta['pages_total']} pages)" if (meta or {}).get("pages_total") else "") + "."
        self.say(nid, text, key=f"read:{src['id']}", refs=[{"kind": "resource", "id": src["id"], "label": src["title"][:50]}])
        for i in (meta or {}).get("issues", []):
            n = len(i["pages"])
            msg = {"drawing_only": f"{n} page{'s' if n != 1 else ''} of it only had lines or shapes and no text.", "blank": f"{n} page{'s' if n != 1 else ''} of it were blank.",
                   "failed": f"{n} page{'s' if n != 1 else ''} of it couldn't be read.", "over_limit": f"I only read the first pages; {n} more were not read."}.get(i["kind"])
            if msg:
                self.say(nid, msg + " Details are under Resources.", key=f"issue:{src['id']}:{i['kind']}", refs=[{"kind": "resource", "id": src["id"], "label": src["title"][:50]}])

    # ---- messages and answers
    @staticmethod
    def _block_text(b: dict) -> str:
        c = b["content"]
        if b["type"] == "notes":
            return c.get("markdown", "")
        if b["type"] == "slides":
            return " ".join(f"{s.get('title', '')}: {'; '.join(s.get('points', []))}" for s in c.get("cards", []))
        if b["type"] == "summary":
            return c.get("text", "") + " " + " ".join(c.get("key_points", []))
        if b["type"] == "visual":
            return f"Diagram: {c.get('caption', '')}. {c.get('alt_text', '')}"
        if b["type"] in ("video", "image", "link"):
            return f"{b['type']}: {c.get('title', '')}"
        return ""

    def _ref_chips(self, nid: str, refs: list[str], ch: Optional[dict], order: Optional[list[str]] = None, web_chunks: Optional[dict] = None) -> list[dict]:
        """Chips under an answer: the chapter it drew on, and the student's resources/pages it cited. Clicking one opens that place.
        `order` is the chunk ids in the order the model saw them: a chip gets its number `n` so a [n] in the answer can link to it."""
        srcs = self.store.sources(nid)
        chips, titles = [], {s["id"]: s["title"] for s in srcs}
        book_url = {s["id"]: s["raw_ref"] for s in srcs if s["kind"] == "book"}
        if ch and not web_chunks:
            chips.append(self._chapter_ref(ch))
        page_chunks = {c["chunk_id"]: c for c in ((ch or {}).get("meta") or {}).get("page_chunks", [])}
        page_chunks.update(web_chunks or {})
        num = lambda r: {"n": order.index(r) + 1} if order and r in order else {}  # noqa: E731
        for r in refs:
            m = re.match(r"^(s[0-9a-f]+)-p(\d+)", r)
            if m and m.group(1) in book_url:
                chips.append({"kind": "link", "url": book_url[m.group(1)], "label": "Book · " + titles[m.group(1)][:34], **num(r)})
            elif m and m.group(1) in titles:
                chips.append({"kind": "resource", "id": m.group(1), "label": f"{titles[m.group(1)][:36]} · p{m.group(2)}", "page": int(m.group(2)), **num(r)})
            elif r in page_chunks:
                chips.append({"kind": "link", "url": page_chunks[r].get("url", ""), "label": (page_chunks[r].get("title") or "web page")[:40], **num(r)})
        seen, out = set(), []
        for c in chips:
            k = (c["kind"], c.get("id") or c.get("url"), c.get("page"))
            if k not in seen:
                seen.add(k)
                out.append(c)
        return out[:5]

    def chat_reply(self, nid: str, learner_id: str, text: str, chapter_id: Optional[int] = None, block_ids: Optional[list[int]] = None, style: str = "",
                   guide: bool = False) -> dict:
        """Answer a question about the notebook from its own sources and the chapter in focus. Returns {answer, refs}."""
        nb = self._nb(nid)
        prof = self.profile(learner_id, nb)
        chs = [c for c in self.store.chapters(nid)]
        words = tokens(text)
        ch = next((c for c in chs if c["id"] == chapter_id), None)
        if ch is None and chs:   # no chapter in focus: the one whose title and summary best match the question
            score = lambda c: overlap(words, f"{c['title']} {c['summary']}")  # noqa: E731
            best = max((c for c in chs if c["status"] == "ready"), key=score, default=None)
            ch = best if best is not None and score(best) > 0 else None
        pool = self._all_chunks(nid, books=True) + (((ch or {}).get("meta") or {}).get("page_chunks") or [])
        pool.sort(key=lambda c: -overlap(words, c["text"]))
        notes = ""
        if ch and ch["status"] == "ready":
            notes = next((b["content"].get("markdown", "") for b in self.store.blocks(ch["id"]) if b["type"] == "notes"), "")
        ctx_parts = []
        for bid in (block_ids or [])[:3]:
            b = self.store.get_block(bid)
            if b:
                ctx_parts.append(self._block_text(b)[:1500])
        if ctx_parts:
            notes = "The student is asking about this part of the course:\n" + "\n".join(ctx_parts) + "\n\n" + notes
        history = self._recent_turns(nid, text)
        used = pool[:6]
        with self._scope(nid):
            res = doubt.run(self.router, text, used, prof, notes, style, history=history, guide=guide)
            out = res.value
            if not out.covered and not ctx_parts:        # the notebook cannot answer: look it up, say so, and cite what was found
                web_pool = self._web_lookup(nb, text)
                if web_pool:
                    r2 = doubt.run(self.router, text, web_pool, prof, "", style, history=history, external=True, guide=guide)
                    by = {c["chunk_id"]: c for c in web_pool}
                    return {"answer": "That isn't in your notebook, so I looked it up online: " + r2.value.answer,
                            "refs": self._ref_chips(nid, r2.value.source_refs or [web_pool[0]["chunk_id"]], None, [c["chunk_id"] for c in web_pool], by)}
                return {"answer": out.answer + "\n\n(That isn't covered in your notebook, and I couldn't look it up online right now.)",
                        "refs": self._ref_chips(nid, out.source_refs, ch, [c["chunk_id"] for c in used])}
        quote = f"\n\nFrom the source: “{out.quote[:300]}”" if out.quote else ""
        return {"answer": out.answer + quote,
                "refs": self._ref_chips(nid, out.source_refs, ch, [c["chunk_id"] for c in used])}

    def _check_coverage(self, nid: str, chunks: list[dict]) -> Optional[dict]:
        """If the learner gave a contents list or syllabus, say whether every item maps to a chapter (and which do not). Also reports structural problems."""
        try:
            chs = self.store.chapters(nid)
            for problem in coverage.validate_outline(chs):
                self.router.note("architect", "fallback", "outline check", problem)
            items = coverage.checklist_from_chunks(chunks)
            if not items:
                return None
            cov = coverage.coverage_map(items, chs)
            self.store.cache_set(f"coverage:{nid}", {**cov, "items": items})
            gaps = cov["gaps"]
            if gaps:
                self.say(nid, f"{len(gaps)} item{'s' if len(gaps) != 1 else ''} in your contents list {'are' if len(gaps) != 1 else 'is'} not covered by a chapter yet: "
                              + "; ".join(g[:40] for g in gaps[:3]) + ("…" if len(gaps) > 3 else "") + ".", key="coverage")
            else:
                self.say(nid, f"Every one of the {len(items)} items in your contents list is covered by a chapter.", key="coverage")
            return cov
        except Exception as e:  # noqa: BLE001  a check must never fail the notebook
            self.router.note("architect", "fallback", "coverage check failed", f"{type(e).__name__}: {e}"[:150])
            return None

    def _source_books(self, nid: str, nb: dict) -> int:
        """Find real books on the topic (Open Library) and keep them as a resource the chat can look things up in, cited as [n] with a link.
        Best effort: no books (offline, nothing found) just means no books; it never fails the notebook."""
        if os.environ.get("AASAAN_BOOKS", "on").strip().lower() in ("off", "0", "false", "no") or any(s["kind"] == "book" for s in self.store.sources(nid)):
            return 0
        try:
            topic = search_topic(nb)
            books = web.discover_books(topic, 5)
        except Exception as e:  # noqa: BLE001
            self.router.note("books", "fallback", "book search failed", f"{type(e).__name__}: {e}"[:150])
            return 0
        for b in books:
            x = b.extra
            by = ", ".join(x.get("authors") or [])
            text = " ".join(p for p in [f"{b.title}" + (f" by {by}" if by else "") + (f" ({x['year']})." if x.get("year") else "."),
                                        f"Topics: {', '.join(x['subjects'])}." if x.get("subjects") else "", f"Opens: \"{x['first_sentence']}\"" if x.get("first_sentence") else "",
                                        "Can be read online through the Internet Archive." if x.get("ia") else ""] if p)
            sid = self.store.add_source(nid, "book", b.url, b.title, text, [])
            self.store.update_source_chunks(sid, [{"chunk_id": f"{sid}-p1-c1", "page": 1, "text": text}])
            self.store.set_source_meta(sid, {"status": "ready", "authors": x.get("authors"), "year": x.get("year"), "ia": x.get("ia")})
        if books:
            self.say(nid, f"I also found {len(books)} book{'s' if len(books) != 1 else ''} on {search_topic(nb)[:40]} that the chat can look things up in.", key="books",
                     refs=[{"kind": "link", "url": b.url, "label": b.title[:40]} for b in books[:3]])
        return len(books)

    def _recent_turns(self, nid: str, current: str, limit: int = 8) -> list[dict]:
        """The last few chat turns (not narration, not proposals), oldest first, so a follow-up like "explain that again" has something to point at."""
        msgs = [m for m in self.store.messages(nid, max(0, self.store.last_message_id(nid) - 40)) if m["kind"] == "chat" and m["content"]]
        if msgs and msgs[-1]["role"] == "user" and msgs[-1]["content"].strip() == current.strip():
            msgs = msgs[:-1]                                    # the question being answered is already stored; it is not history
        return [{"role": m["role"], "text": m["content"][:500]} for m in msgs[-limit:]]

    def _web_lookup(self, nb: dict, question: str) -> list[dict]:
        """Up to 2 web pages about the question, as chunks w1, w2. The search query is the notebook's subject plus a few keywords,
        never the learner's whole message. Any failure means no pages (the caller says so)."""
        try:
            q = (search_topic(nb) + " " + " ".join(sorted(tokens(question, 3))[:5])).strip()
            cands = web.discover_wikipedia(q, 3)
            if os.environ.get("FIRECRAWL_API_KEY"):
                cands += web.discover_firecrawl(q, 3)
            out: list[dict] = []
            for c in cands:
                page = web.fetch_page(c.url, self.store)
                if page.ok and page.text:
                    out.append({"chunk_id": f"w{len(out) + 1}", "text": page.text[:1200], "url": c.url, "title": page.title or c.title})
                if len(out) >= 2:
                    break
            return out
        except Exception as e:  # noqa: BLE001
            self.router.note("doubt", "fallback", "online lookup failed", f"{type(e).__name__}: {e}"[:150])
            return []

    # ---- one chat turn: what does the student want, and do it
    def handle_chat(self, nid: str, learner_id: str, question: str, chapter_id: Optional[int] = None, block_ids: Optional[list[int]] = None, mode: str = "answer") -> dict:
        """{"type": "text", "text", "refs"} (stored by the caller) or {"type": "proposal", ...} (already stored as a chat card)."""
        chs = self.store.chapters(nid)
        intent = gates.route_intent(self.router, question, {"message": question[:500], "chapters": [c["title"] for c in chs][:25]})
        kind = intent["intent"]
        self.router.note("intent", "ok", f"chat intent: {kind}", f"decided by {intent['by']}" + (" (unsure: asked to clarify)" if intent["unsure"] else ""))
        if intent["unsure"]:
            return {"type": "text", "text": "Do you want me to answer a question about your course, or change the course?", "refs": []}
        if kind == "other":
            refusal = gates.input_gate(self.router, question)
            return {"type": "text", "refs": [], "text": refusal or "I can help with your course: ask me a question about it, add a file or link, or ask me to change the course."}
        if kind == "add_resource":
            return {"type": "text", "refs": [], "text": "Attach the file or paste the link here, or use Add a resource under Resources, and I'll read it."}
        if kind == "quiz_me":
            ch = self._focus_chapter(chs, question, chapter_id)
            if not ch or ch["status"] != "ready":
                return {"type": "text", "refs": [], "text": "Pick a chapter that is ready and I'll open its quiz."}
            return {"type": "text", "text": f"Here is the quiz for chapter {ch['position']}. Answer it on the course page.", "refs": [{**self._chapter_ref(ch), "quiz": True}]}
        if kind == "edit_course":
            p = edits.propose(self, nid, learner_id, question)
            if p is None:
                return {"type": "text", "refs": [], "text": "I couldn't turn that into a change to the course. Try something like \"add a chapter on Delta Lake after chapter 5\"."}
            return {"type": "proposal", "proposal": p}
        out = self.chat_reply(nid, learner_id, question, chapter_id, block_ids, style="explain" if kind == "explain" else "", guide=(mode == "guide"))
        return {"type": "text", "text": out["answer"], "refs": out["refs"]}

    @staticmethod
    def _focus_chapter(chs: list[dict], text: str, chapter_id: Optional[int]) -> Optional[dict]:
        ch = next((c for c in chs if c["id"] == chapter_id), None)
        words = tokens(text)
        m = re.search(r"chapter\s+(\d+)", text.lower())
        if m:
            ch = next((c for c in chs if c["position"] == int(m.group(1))), ch)
        elif ch is None:
            score = lambda c: overlap(words, f"{c['title']} {c['summary']}")  # noqa: E731
            ch = max(chs, key=score, default=None)
        return ch

    def _assert_acyclic(self, nid: str) -> None:
        graph = {c["id"]: [p for p in c["prereqs"]] for c in self.store.chapters(nid)}
        state: dict = {}

        def visit(n):
            if state.get(n) == 1:
                raise ValueError("prerequisite cycle")
            if state.get(n) == 2 or n not in graph:
                return
            state[n] = 1
            for p in graph[n]:
                visit(p)
            state[n] = 2

        for n in graph:
            visit(n)

    # ---- proposals (apply / undo / dismiss live in edits.py)
    def proposal(self, nid: str, pid: int) -> dict:
        p = self.store.get_proposal(pid)
        if not p or p["notebook_id"] != nid:
            raise UserError("unknown proposal")
        return edits.view(self.store, p)

    def apply_proposal(self, nid: str, pid: int) -> dict:
        return edits.apply(self, nid, pid)

    def undo_proposal(self, nid: str, pid: int) -> dict:
        return edits.undo(self, nid, pid)

    def dismiss_proposal(self, nid: str, pid: int) -> dict:
        return edits.dismiss(self, nid, pid)

    def chat_history(self, nid: str, after: int = 0) -> list[dict]:   # replaces the plain version: proposal cards carry their current state
        self._nb(nid)
        out = self.store.messages(nid, after)
        for m in out:
            if m["kind"] == "proposal" and m["refs"]:
                p = self.store.get_proposal(m["refs"][0]["id"])
                if p:
                    m["proposal"] = edits.view(self.store, p)
        return out

    def _propose_for_resource(self, nid: str, src: dict, topics: list[str]) -> None:
        """A newly read resource may extend the course. It is only ever a proposal: nothing changes until the student applies it."""
        known = " ".join(c["title"].lower() for c in self.store.chapters(nid))
        new = [t for t in topics if not set(re.findall(r"\w{4,}", t.lower())) & set(re.findall(r"\w{4,}", known))]
        label = f'"{src["title"][:50]}"'
        if not topics or not new:
            self.say(nid, f"{label} fits your current course, so I'm not suggesting any changes.", key=f"fit:{src['id']}")
            return
        self.say(nid, f"{label} also covers {', '.join(new[:3])}. Here is a suggestion.", key=f"fit:{src['id']}")
        request = f"The student added a resource named {src['title'][:60]} covering: {'; '.join(new[:4])}. Propose adding chapters only for what the course does not cover yet."
        nb = self._nb(nid)
        edits.propose(self, nid, nb["learner_id"], request, source="resource", resources=[src["title"]])

    # ============================================================ added resources
    MAX_RESOURCES = 20
    TEXT_EXT = (".txt", ".md", ".markdown")

    def validate_resources(self, nid: str, files: list[tuple[str, str, bytes]], urls: list[str]) -> None:
        self._nb(nid)
        if len(self.store.sources(nid)) + len(files) + len(urls) > self.MAX_RESOURCES:
            raise UserError(f"A notebook can hold up to {self.MAX_RESOURCES} resources.")
        for name, ctype, data in files:
            low = name.lower()
            if len(data) > MAX_FILE_BYTES:
                raise UserError(f"{name} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
            if not (ctype == "application/pdf" or low.endswith(".pdf") or ctype.startswith("image/") or ctype.startswith("text/") or low.endswith(self.TEXT_EXT)):
                raise UserError(f"{name}: only PDFs, photos and text files can be added.")
        for u in urls:
            ok, why = web.is_safe_url(u)
            if not ok:
                raise UserError(f"I can't open that link: {why}")

    def add_pasted_text(self, nid: str, text: str) -> dict:
        self._nb(nid)
        if len(self.store.sources(nid)) >= self.MAX_RESOURCES:
            raise UserError(f"A notebook can hold up to {self.MAX_RESOURCES} resources.")
        sid = self.store.add_source(nid, "text", "pasted", "Pasted notes", text[:200_000], [])
        return self._queue_resource(nid, sid, "text", "Pasted notes", len(text))

    def add_resources(self, nid: str, files: list[tuple[str, str, bytes]], urls: list[str]) -> list[dict]:
        """Chat or Resources view: new files and links become sources and are read in the background. The course is NOT changed here."""
        self.validate_resources(nid, files, urls)
        out = []
        folder = self.upload_dir / nid
        for name, ctype, data in files:
            if len(data) > MAX_FILE_BYTES:
                raise UserError(f"{name} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
            low = name.lower()
            is_pdf = ctype == "application/pdf" or low.endswith(".pdf")
            is_text = ctype.startswith("text/") or low.endswith(self.TEXT_EXT)
            if not (is_pdf or ctype.startswith("image/") or is_text):
                raise UserError(f"{name}: only PDFs, photos and text files can be added.")
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / re.sub(r"[^\w.\-]", "_", name)[:80]
            path.write_bytes(data)
            if is_text and not is_pdf:
                sid = self.store.add_source(nid, "text", str(path), name, data.decode("utf-8", "replace")[:200_000], [])
                kind = "text"
            else:
                kind = "pdf" if is_pdf else "image"
                sid = self.store.add_source(nid, kind, str(path), name, "", [])
            out.append(self._queue_resource(nid, sid, kind, name, len(data)))
        for u in urls:
            ok, why = web.is_safe_url(u)
            if not ok:
                raise UserError(f"I can't open that link: {why}")
            sid = self.store.add_source(nid, "url", u, u, "", [])
            out.append(self._queue_resource(nid, sid, "url", u, 0))
        return out

    def _queue_resource(self, nid: str, sid: str, kind: str, name: str, size: int) -> dict:
        self.store.set_source_meta(sid, {"status": "reading", "size": size})
        self.store.add_job(nid, None, "resource", {"source_id": sid})
        self.say(nid, f"Got \"{name[:50]}\". Reading it now.", key=f"got:{sid}", refs=[{"kind": "resource", "id": sid, "label": name[:50]}])
        return {"id": sid, "name": name, "kind": kind, "status": "reading", "size": size}

    def process_resource(self, nid: str, sid: str) -> None:
        with self._scope(nid):
            nb = self._nb(nid)
            src = next((s for s in self.store.sources(nid) if s["id"] == sid), None)
            if not src:
                return
            warnings: list[str] = []
            got = self._read_source(src, nb, warnings)
            meta = (next((s for s in self.store.sources(nid) if s["id"] == sid), src).get("meta")) or {}
            if got is None:
                meta = {**meta, "status": "unreadable", "issues": meta.get("issues") or [{"kind": "failed", "pages": [], "checked_with_vision": False,
                                                                                         "reason": (warnings[0] if warnings else "It could not be read.")[:200]}]}
                self.store.set_source_meta(sid, meta)
                self._say_source_read(nid, src, meta, 0)
                return
            self.store.update_source_chunks(sid, [c.model_dump() for c in got.chunks])
            if not meta.get("status") or meta.get("status") == "reading":
                meta = {**meta, "status": "ready", "pages_total": meta.get("pages_total"), "issues": meta.get("issues", [])}
            self.store.set_source_meta(sid, meta)
            self._say_source_read(nid, src, meta, len(got.chunks))
            if meta.get("status") != "unreadable" and self.store.chapters(nid):
                try:
                    self._propose_for_resource(nid, src, [t.title for t in got.topics])
                except Exception as e:  # noqa: BLE001  a failed suggestion never undoes the reading
                    log.warning("[aasaan] could not propose course changes for %s: %s", src["id"], e)

    # ========================================================= resources view
    def _used_in(self, nid: str, sid: str, chapters: Optional[list] = None, notes_refs: Optional[dict] = None) -> list[dict]:
        """Chapters that cite this source (their outline refs or the refs the notes were written from)."""
        out = []
        for ch in chapters if chapters is not None else self.store.chapters(nid):
            refs = list(ch.get("source_refs") or []) + list((notes_refs or {}).get(ch["id"], []))
            if any(str(r).startswith(sid + "-") for r in refs):
                out.append({"id": ch["id"], "position": ch["position"], "title": ch["title"][:60]})
        return out

    def _notes_refs(self, chs: list[dict]) -> dict:
        out = {}
        for ch in chs:
            if ch["status"] == "ready":
                nb_ = next((b for b in self.store.blocks(ch["id"]) if b["type"] == "notes"), None)
                out[ch["id"]] = list(((nb_ or {}).get("meta") or {}).get("refs", []))
        return out

    @staticmethod
    def _summary_of(src: dict) -> str:
        """A short, plain description of what we read (our own words are not needed: this is the start of their text, trimmed)."""
        text = (src.get("extracted_text") or " ".join(c.get("text", "") for c in src.get("chunks", [])[:3]) or "").strip()
        text = re.sub(r"\s+", " ", text)
        return (text[:170].rsplit(" ", 1)[0] + "…") if len(text) > 170 else text

    def resources_view(self, nid: str) -> dict:
        """Everything the student gave (files, links, pasted text) and everything Aasaan found for the chapters, with statuses."""
        self._nb(nid)
        chs = self.store.chapters(nid)
        notes_refs = self._notes_refs(chs)
        mine, books = [], []
        for s in self.store.sources(nid):
            meta = s.get("meta") or {}
            if s["kind"] == "book":
                books.append({"kind": "book", "title": s["title"], "source": "openlibrary.org" + (" · " + ", ".join(meta["authors"][:2]) if meta.get("authors") else ""),
                              "url": s["raw_ref"], "license": "", "author": str(meta.get("year") or ""), "used_in": []})
                continue
            kind = s["kind"]
            size = meta.get("size") or len(s.get("extracted_text") or "") if kind == "text" else meta.get("size", 0)
            mine.append({"id": s["id"], "kind": "pasted" if (kind == "text" and s["raw_ref"] == "pasted") else kind, "title": s["title"],
                         "url": s["raw_ref"] if kind == "url" else "", "size": size, "status": meta.get("status", "ready"),
                         "pages_total": meta.get("pages_total"), "pages_read": meta.get("pages_read"), "issues": meta.get("issues", []),
                         "length": len(s.get("extracted_text") or "") if kind == "text" else None,
                         "summary": self._summary_of(s), "has_file": kind in ("pdf", "image") and bool(s["raw_ref"]),
                         "used_in": self._used_in(nid, s["id"], chs, notes_refs)})
        found: dict[str, dict] = {}
        for ch in chs:
            if ch["status"] != "ready":
                continue
            for b in self.store.blocks(ch["id"]):
                c = b["content"]
                if b["type"] == "video":
                    key, item = "video:" + c.get("video_id", ""), {"kind": "video", "title": c.get("title", ""), "source": c.get("channel", "YouTube"), "url": c.get("url", ""), "license": "", "author": ""}
                elif b["type"] == "link":
                    key, item = "link:" + c.get("url", ""), {"kind": "article", "title": c.get("title", ""), "source": c.get("source", ""), "url": c.get("url", ""), "license": "", "author": ""}
                elif b["type"] == "image":
                    a = c.get("attribution", {})
                    key, item = "image:" + c.get("url", ""), {"kind": "image", "title": c.get("title", ""), "source": a.get("source", ""), "url": a.get("page_url") or c.get("url", ""), "thumb": c.get("url", ""),
                                                            "license": a.get("license", ""), "author": a.get("author", "")}
                elif b["type"] == "visual" and c.get("kind") == "figure":
                    a = c.get("attribution") or {}
                    key, item = "fig:" + (c.get("payload") or {}).get("url", ""), {"kind": "image", "title": c.get("caption", ""), "source": a.get("source", ""), "url": a.get("page_url", ""),
                                                                                  "thumb": (c.get("payload") or {}).get("url", ""), "license": a.get("license", ""), "author": a.get("author", "")}
                else:
                    continue
                cur = found.setdefault(key, {**item, "used_in": []})
                cur["used_in"].append({"id": ch["id"], "position": ch["position"], "title": ch["title"][:60]})
        return {"mine": mine, "found": list(found.values()) + books}

    def resource_impact(self, nid: str, sid: str) -> dict:
        chs = self.store.chapters(nid)
        return {"cited_by": self._used_in(nid, sid, chs, self._notes_refs(chs))}

    def _source_or_404(self, nid: str, sid: str) -> dict:
        src = next((s for s in self.store.sources(nid) if s["id"] == sid), None)
        if not src:
            raise UserError("unknown resource")
        return src

    def remove_resource(self, nid: str, sid: str, regenerate: bool = False) -> dict:
        """Remove a source. The chapters that cite it keep their text; with `regenerate` they are rebuilt without it."""
        src = self._source_or_404(nid, sid)
        impact = self.resource_impact(nid, sid)["cited_by"]
        self.store.delete_source(sid)
        if src["kind"] in ("pdf", "image") and src["raw_ref"]:
            try:
                Path(src["raw_ref"]).unlink(missing_ok=True)
            except OSError:
                pass
        for c in impact:
            ch = self._chapter(c["id"])
            self.store.update_chapter(c["id"], summary=ch["summary"])
            self.store.set_chapter_refs(c["id"], [r for r in ch.get("source_refs", []) if not str(r).startswith(sid + "-")])
            if regenerate:
                self.store.set_chapter_status(c["id"], "queued")
                self.store.update_chapter(c["id"], priority=60)
                self.store.add_job(nid, c["id"], "chapter")
        self.say(nid, f"Removed \"{src['title'][:50]}\"." + (f" Rebuilding {len(impact)} chapter{'s' if len(impact) != 1 else ''} without it." if regenerate and impact else ""),
                 key=f"removed:{sid}")
        return {"removed": sid, "regenerating": [c["id"] for c in impact] if regenerate else []}

    def reprocess_resource(self, nid: str, sid: str) -> dict:
        src = self._source_or_404(nid, sid)
        self.store.set_source_meta(sid, {"status": "reading", "size": (src.get("meta") or {}).get("size", 0)})
        self.store.add_job(nid, None, "resource", {"source_id": sid})
        self.say(nid, f"Reading \"{src['title'][:50]}\" again.", key=f"re:{sid}:{self.store.last_message_id(nid)}")
        return {"id": sid, "status": "reading"}

    def resource_file(self, nid: str, sid: str) -> Path:
        src = self._source_or_404(nid, sid)
        p = Path(src["raw_ref"]).resolve()
        if src["kind"] not in ("pdf", "image") or not p.is_file() or self.upload_dir.resolve() not in p.parents:
            raise UserError("no file for this resource")
        return p

    def resource_thumb(self, nid: str, sid: str) -> Optional[bytes]:
        """PNG thumbnail: the image itself, or the first page of a PDF (needs pypdfium2; None when unavailable)."""
        src = self._source_or_404(nid, sid)
        p = self.resource_file(nid, sid)
        if src["kind"] == "image":
            return p.read_bytes()
        return intake.render_page_png(p.read_bytes(), 1, scale=0.5)

    def resource_preview(self, nid: str, sid: str) -> dict:
        src = self._source_or_404(nid, sid)
        text = src.get("extracted_text") or "\n\n".join(c.get("text", "") for c in src.get("chunks", [])[:6])
        return {"id": sid, "title": src["title"], "kind": src["kind"], "text": text[:4000], "truncated": len(text) > 4000}

    # ============================================================== chapters
    def build_chapter(self, cid: int) -> None:
        with self._scope(self._chapter(cid)["notebook_id"]):
            return self._build_chapter(cid)

    def _build_chapter(self, cid: int) -> None:
        """Curator -> Teacher <-> Critic -> Quizzer, then write all blocks and flip to ready in one transaction."""
        ch = self._chapter(cid)
        nb = self._nb(ch["notebook_id"])
        prof = self.profile(nb["learner_id"], nb)
        self.store.set_chapter_status(cid, "drafting")
        all_chunks = self._all_chunks(nb["id"])
        own = self._chapter_chunks(ch, all_chunks)

        res = curator.curate(self.router, self.store, ch["title"], ch["objectives"], search_topic(nb), nb["language"])
        self._dec(nb["id"], "curator", "chose", f"resources for chapter {ch['position']}", chapter_id=cid,
                  counts={"videos": len(res.videos), "links": len(res.links), "images": len(res.images)}, dedupe_key=f"curate|{cid}|{int(time.time() // 5)}")
        page_chunks = [{"chunk_id": p["chunk_id"], "page": 1, "text": p["text"], "source_id": "web"} for p in res.pages]
        chunks = own + page_chunks
        link_refs = [{"ref": f"url:{c.url}", "title": c.title, "source": c.source, "kind": c.kind} for c in res.links + res.videos]
        allowed_extra = {l["ref"] for l in link_refs}

        issues, previous, content, verified, revisions, open_points = None, None, None, False, 0, []
        claims_total = claims_flagged = None
        quiz_future = None
        dprof = depth_mod.profile(self.store, nb["id"])
        self._sh(nb["id"], "chapter_start", cid, ch["position"])
        with ThreadPoolExecutor(max_workers=2) as pool:
            for attempt in range(MAX_REVISIONS + 1):
                content = teacher.run(self.router, ch, chunks, link_refs, prof, issues, previous, self.store, depth=dprof).value
                self._sh(nb["id"], "draft", cid)
                self.store.set_chapter_status(cid, "checking")
                # the quiz is written while the Critic checks this draft (saves a full round trip on the happy path)
                quiz_future = pool.submit(quizzer.run, self.router, ch, content, chunks, prof, self.store)
                review: CriticOut = critic.run(self.router, content, chunks, prof, allowed_extra).value
                claims_total, claims_flagged = review.claims_total, review.claims_flagged
                if review.verdict == "approve":
                    verified = True
                    self._sh(nb["id"], "approved", cid, True)
                    break
                open_points = [i.model_dump() for i in review.issues]
                if attempt == MAX_REVISIONS:
                    self.router.note("critic", "fallback", f"Chapter '{ch['title'][:40]}' shipped unverified",
                                     f"Critic still had {len(review.issues)} issue(s) after {MAX_REVISIONS} revisions")
                    self._sh(nb["id"], "approved", cid, False)
                    break
                self._sh(nb["id"], "fixes", cid, len(review.issues))
                revisions += 1
                issues = [i.model_dump() for i in review.issues]
                previous = content.model_dump()
                self.store.set_chapter_status(cid, "drafting")
            assert content is not None and quiz_future is not None
            # visuals are made from the final notes while the quiz finishes; they can only add to a chapter, never block it
            illo_pool = ThreadPoolExecutor(max_workers=1)
            illo_future = illo_pool.submit(contextvars.copy_context().run, illustrator.run, self.router, self.store, ch["title"],
                                           content.notes_md, prof.language)
            scope_ = self.store.cache_get(f"scope:{nb['id']}") or {}
            practice_future = slides_pool_ = None
            if scope_.get("track") in ("dsa", "exam") and os.environ.get("AASAAN_PRACTICE", "on").strip().lower() not in ("off", "0", "false", "no"):
                slides_pool_ = ThreadPoolExecutor(max_workers=1)
                practice_future = slides_pool_.submit(contextvars.copy_context().run, practice_agent.practice_for, scope_.get("track"), ch["title"], prof.level, nb["goal"] or "")
            slides_pool = ThreadPoolExecutor(max_workers=1)        # the deck is written from the FINAL notes, beside the quiz and visuals; it can only add, never block
            slides_future = None if os.environ.get("AASAAN_SLIDES", "on").strip().lower() in ("off", "0", "false", "no") else slides_pool.submit(
                contextvars.copy_context().run, slides_agent.run, self.router, ch, content, prof, dprof)
            quiz: QuizOut = quiz_future.result().value  # the quiz of the FINAL draft (earlier drafts' quizzes are discarded)
            quiz, quiz_notes = gates.quiz_sanity(self.router, quiz, content.notes_md)
            for n in quiz_notes:
                self.router.note("quizzer", "fallback", f"quiz_sanity on '{ch['title'][:40]}'", n)
        visuals = []
        try:
            visuals = illo_future.result(timeout=float(os.environ.get("AASAAN_ILLUSTRATOR_TIMEOUT_S", "90"))).blocks
        except FutureTimeout:
            self.router.note("illustrator", "failed", "Illustrator timed out; chapter ships without visuals", "AASAAN_ILLUSTRATOR_TIMEOUT_S exceeded")
        except Exception as e:  # noqa: BLE001
            self.router.note("illustrator", "failed", "Illustrator failed; chapter ships without visuals", f"{type(e).__name__}: {e}")
        illo_pool.shutdown(wait=False, cancel_futures=True)
        practice_content = None
        if practice_future is not None:
            try:
                practice_content = practice_future.result(timeout=float(os.environ.get("AASAAN_PRACTICE_TIMEOUT_S", "15")))
            except Exception:  # noqa: BLE001  practice is a bonus: a timeout or failure means no practice block
                practice_content = None
            slides_pool_.shutdown(wait=False, cancel_futures=True)
            if practice_content:
                self._dec(nb["id"], "practice", "chose", f"{len(practice_content['items'])} practice items", chapter_id=cid, counts={"items": len(practice_content["items"])},
                          dedupe_key=f"practice|{cid}|{int(time.time() // 5)}")
        deck_cards = None
        if slides_future is not None:
            try:
                deck_cards = [c.model_dump() for c in slides_future.result(timeout=float(os.environ.get("AASAAN_SLIDES_TIMEOUT_S", "90"))).value.slides]
                self._dec(nb["id"], "slides", "accepted", f"{len(deck_cards)} slides", chapter_id=cid, counts={"slides": len(deck_cards)}, dedupe_key=f"slides|{cid}|{int(time.time() // 5)}")
            except FutureTimeout:
                self.router.note("slides", "failed", "slides timed out; the Teacher's own slides are used", "AASAAN_SLIDES_TIMEOUT_S exceeded")
            except Exception as e:  # noqa: BLE001
                self.router.note("slides", "failed", "slides failed; the Teacher's own slides are used", f"{type(e).__name__}: {e}"[:150])
            slides_pool.shutdown(wait=False, cancel_futures=True)

        notes_clean, read_meta = readability.clean_notes(content.notes_md, ch["title"])
        if depth_mod.needs_expansion(notes_clean, dprof) and self.router.fake is None and not os.environ.get("AASAAN_NO_EXPAND"):
            try:                                                   # one expansion pass: add examples, an edge case, a common mistake; then it is re-checked by size only
                longer = teacher.run(self.router, ch, chunks, link_refs, prof, [{"step": 0, "problem": f"The notes are too short ({read_meta['words']} words; about "
                                                                                    f"{dprof['words']} are needed). Add worked examples, an edge case and a common mistake."}],
                                     content.model_dump(), self.store, depth=dprof).value
                n2, m2 = readability.clean_notes(longer.notes_md, ch["title"])
                if m2["words"] > read_meta["words"]:
                    content, notes_clean, read_meta = longer, n2, m2
                    self._dec(nb["id"], "teacher", "expanded", f"chapter {ch['position']}", chapter_id=cid, counts={"words": m2["words"]}, reason_code="too_short",
                              dedupe_key=f"expand|{cid}|{int(time.time() // 5)}")
            except Exception as e:  # noqa: BLE001  an expansion is a bonus, never a failure
                self.router.note("teacher", "fallback", f"expansion of '{ch['title'][:40]}' skipped", f"{type(e).__name__}: {e}"[:150])
        blocks = [
            {"type": "notes", "content": {"markdown": notes_clean}, "meta": {"refs": content.source_refs}},
            {"type": "slides", "content": {"cards": deck_cards or [s.model_dump(exclude={"image_query"}) for s in content.slides]}},
        ]
        blocks[1:1] = visuals   # pictures sit right after the notes they illustrate
        for v in res.videos:
            blocks.append({"type": "video", "verified": True, "content": {"video_id": v.extra["video_id"], "title": v.title, "channel": v.source,
                                                                        "url": v.url, "start": v.extra.get("start", 0)}})
        for im in res.images:
            blocks.append({"type": "image", "verified": True, "content": {
                "url": im.url, "title": im.title, "attribution": {"author": im.extra.get("author", ""), "license": im.extra.get("license", ""),
                                                                  "source": im.source, "page_url": im.extra.get("page_url", "")}}})
        for ln in res.links:
            blocks.append({"type": "link", "verified": True, "content": {"url": ln.url, "title": ln.title, "source": ln.source, "reason": ln.reason}})
        if practice_content:
            blocks.append({"type": "practice", "verified": True, "content": practice_content})
        blocks.append({"type": "quiz", "content": {"questions": [q.model_dump() for q in quiz.questions]}})
        blocks.append({"type": "summary", "content": {"text": content.summary, "key_points": content.key_points}})
        meta = {**ch["meta"], "unverified": not verified, "grounded": bool(chunks), "revisions": revisions, "readability": read_meta, "depth": dprof["name"],
                "claims": ({"total": claims_total, "verified": claims_total - (0 if verified else (claims_flagged or 0))} if claims_total is not None else None),
                "common_mistakes": content.common_mistakes,
                "page_chunks": page_chunks, "curator_notes": res.notes,
                "review_issues": [] if verified else self._claim_rows(open_points, chunks, allowed_extra)}
        self.store.replace_blocks(cid, blocks, "ready", meta)
        nid_ = nb["id"]
        self._sh(nid_, "outputs_ready", cid)
        self._dec(nid_, "teacher", "accepted", f"notes for chapter {ch['position']}", chapter_id=cid, counts={"words": len(content.notes_md.split())},
                  dedupe_key=f"notes|{cid}|{int(time.time() // 5)}")
        if revisions:
            self._dec(nid_, "critic", "fixed", f"chapter {ch['position']}", chapter_id=cid, counts={"revisions": revisions}, reason_text="the Critic sent claims back to the Teacher",
                      dedupe_key=f"fix|{cid}|{int(time.time() // 5)}")
        self._dec(nid_, "critic", "accepted" if verified else "stopped", f"chapter {ch['position']}" + ("" if verified else " as unverified"), chapter_id=cid,
                  reason_code=None if verified else "loop_limit", outcome="ok" if verified else "partial", severity="info" if verified else "notice",
                  dedupe_key=f"verdict|{cid}|{int(time.time() // 5)}")
        self._dec(nid_, "quizzer", "accepted", f"{len(quiz.questions)} quiz questions", chapter_id=cid, counts={"questions": len(quiz.questions)}, dedupe_key=f"quiz|{cid}|{int(time.time() // 5)}")
        self._announce_chapter(ch, verified)
        if verified:
            self.store.cache_set(teacher.content_cache_key(ch, prof.language), content.model_dump())
            self.store.cache_set(quizzer.quiz_cache_key(ch, prof.language), quiz.model_dump())
        self._refresh_status(nb["id"])

    def _refresh_status(self, nid: str) -> None:
        """generating once chapter 1 is ready; complete when every chapter is ready. Outlining, first-chapter and
        failed states are owned by the job code."""
        nb = self.store.get_notebook(nid)
        chs = self.store.chapters(nid)
        if not nb or not chs:
            return
        if all(c["status"] == "ready" for c in chs):
            status = "complete"
        elif chs[0]["status"] == "ready":
            status = "generating"
        else:
            return
        if status != nb["status"] or nb.get("error"):
            self.store.update_notebook(nid, status=status, error="")
        if status == "complete":
            self.say(nid, f"Your whole notebook is ready: {len(chs)} chapters.", key="complete")

    def prioritize(self, cid: int) -> dict:
        """The student opened a chapter that is not ready: jump the queue."""
        ch = self._chapter(cid)
        if ch["status"] in ("queued", "drafting", "checking"):
            self.store.update_chapter(cid, priority=50)
        return {"id": cid, "priority": 50, "status": ch["status"]}

    def retry_chapter(self, cid: int) -> None:
        ch = self._chapter(cid)
        nb = self._nb(ch["notebook_id"])
        if ch["status"] != "failed":
            return
        if nb["status"] == "failed" and ch["position"] == 1:
            return self.retry_notebook(nb["id"])
        self.store.set_chapter_status(cid, "queued")
        self.store.update_chapter(cid, priority=60)
        self.store.add_job(nb["id"], cid, "chapter")

    def recheck_chapter(self, cid: int) -> dict:
        """"Re-check": run the Critic again over the chapter exactly as stored (including the student's own edits)."""
        ch = self._chapter(cid)
        if ch["status"] != "ready":
            raise UserError("This chapter is still being prepared.")
        with self._scope(ch["notebook_id"]):
            nb = self._nb(ch["notebook_id"])
            prof = self.profile(nb["learner_id"], nb)
            blocks = self.store.blocks(cid)
            notes = next((b for b in blocks if b["type"] == "notes"), None)
            if not notes:
                raise UserError("This chapter has no notes to check.")
            slides = next((b["content"].get("cards", []) for b in blocks if b["type"] == "slides"), [])
            summ = next((b["content"] for b in blocks if b["type"] == "summary"), {})
            data = {"notes_md": notes["content"].get("markdown", ""), "slides": [{"title": s.get("title", ""), "points": s.get("points", [])} for s in slides],
                    "key_points": summ.get("key_points", []), "summary": summ.get("text", ""), "source_refs": (notes["meta"] or {}).get("refs", [])}
            try:
                content = ChapterContent.model_validate(data)
            except ValueError:   # the student's edits can break the strict shape (very short notes); the Critic only reads the text
                content = ChapterContent.model_construct(**data)
            chunks = self._chapter_chunks(ch, self._all_chunks(nb["id"])) + (ch["meta"].get("page_chunks") or [])
            allowed = {f"url:{b['content'].get('url')}" for b in blocks if b["type"] in ("link", "video") and b["content"].get("url")}
            review = critic.run(self.router, content, chunks, prof, allowed).value
            verified = review.verdict == "approve"
            rows = [] if verified else self._claim_rows([i.model_dump() for i in review.issues], chunks, allowed)
            self.store.update_chapter(cid, meta={**ch["meta"], "unverified": not verified, "review_issues": rows})
            return {"verified": verified, "review_issues": rows}

    def retry_notebook(self, nid: str) -> None:
        """"Try again" on the Loading screen: re-run the outline and/or the first chapter."""
        nb = self._nb(nid)
        if nb["status"] != "failed":
            return
        self.store.update_notebook(nid, status="outlining", error="")
        chs = self.store.chapters(nid)
        if chs and chs[0]["status"] == "failed":
            self.store.set_chapter_status(chs[0]["id"], "queued")
        self.store.add_job(nid, None, "notebook")

    # =============================================================== views
    GENERATION = {"queued": "queued", "drafting": "writing", "checking": "checking", "ready": "ready", "failed": "failed"}

    @classmethod
    def _generation(cls, ch: dict) -> str:
        """Where the chapter is in being built: queued, writing, checking, ready, failed. Shown only while it is not ready."""
        return cls.GENERATION.get(ch["status"], "queued")

    @staticmethod
    def _learning(ch: dict) -> str:
        """Where the STUDENT is with it: not_started, in_progress, done, needs_review. Independent of generation."""
        if ch["needs_review"]:
            return "needs_review"
        if ch["completed"]:
            return "done"
        return "in_progress" if ch["started"] else "not_started"

    @classmethod
    def _state(cls, ch: dict) -> str:
        """One value for colouring the graph: failed, generating (not ready yet), else the learning state."""
        if ch["status"] == "failed":
            return "failed"
        if ch["status"] != "ready":
            return "generating"
        return cls._learning(ch)

    @staticmethod
    def _claim_rows(points: list[dict], chunks: list[dict], allowed_extra: set) -> list[dict]:
        """What the Critic could not verify, with whether the source it points at exists (shown under "See which claims")."""
        known = {c["chunk_id"] for c in chunks} | set(allowed_extra)
        rows = []
        for p in points[:12]:
            ref = p.get("source_ref")
            rows.append({"claim": str(p.get("problem", ""))[:400], "where": str(p.get("step", "")), "source_ref": ref,
                         "source_status": "no source cited" if not ref else ("source found" if ref in known else "cited source not in this notebook")})
        return rows

    def notebook_view(self, nid: str) -> dict:
        nb = self._nb(nid)
        chs = self.store.chapters(nid)
        weights = [READINESS_WEIGHT.get(c["status"], 0) for c in chs]
        readiness = round(100 * sum(weights) / len(chs)) if chs else 0
        done = sum(1 for c in chs if c["completed"])
        view_chs = [{"id": c["id"], "position": c["position"], "title": c["title"], "summary": c["summary"],
                     "objectives": c["objectives"], "prereqs": c["prereqs"], "status": c["status"], "state": self._state(c),
                     "learning": self._learning(c), "generation": self._generation(c),
                     "kind": c["kind"], "needs_review": c["needs_review"], "completed": c["completed"], "error": c["error"]}
                    for c in chs]
        edges = [[p, c["id"]] for c in chs for p in c["prereqs"]]
        first_ready = bool(chs) and chs[0]["status"] == "ready"
        return {"id": nid, "cover": cover_image(nid), "title": nb["title"], "goal": nb["goal"], "description": nb["description"], "level": nb["level"],
                "language": nb["language"], "status": nb["status"], "error": nb["error"],
                "chapters": view_chs, "edges": edges, "readiness": readiness, "open_ready": first_ready,
                "your_progress": {"done": done, "total": len(chs), "pct": round(100 * done / len(chs)) if chs else 0},
                "preparing": {"ready": sum(1 for c in chs if c["status"] == "ready"), "total": len(chs),
                              "pct": round(100 * sum(1 for c in chs if c["status"] == "ready") / len(chs)) if chs else 0,
                              "complete": bool(chs) and all(c["status"] == "ready" for c in chs)},
                "progress": {"done": done, "total": len(chs)}, "message_seq": self.store.last_message_id(nid), "decision_seq": self.decisions.last_id(nid), **edits.snapshot_info(self.store, nid), "warnings": [w for w in (self.store.cache_get(f"nbwarn:{nid}") or []) if not re.match(r"page \d+ unreadable or empty$", w)],
                "source_issues": [{"source_id": s["id"], "title": s["title"], "status": s["meta"].get("status", "ready"), "pages_total": s["meta"].get("pages_total"),
                                   "pages_read": s["meta"].get("pages_read"), "issues": s["meta"].get("issues", [])}
                                  for s in self.store.sources(nid) if (s["meta"] or {}).get("issues")],
                "sources": [{"id": src["id"], "kind": src["kind"], "title": src["title"], "status": (src["meta"] or {}).get("status", "ready"),
                             "url": src["raw_ref"] if src["kind"] == "url" else "", "chunks": len(src["chunks"])}
                            for src in self.store.sources(nid)],
                "mode": self.router.mode, "generating": self.store.active_jobs(nid) > 0,
                "next_chapter": next((c["id"] for c in chs if c["status"] == "ready" and not c["completed"]), None)}

    def list_notebooks(self, learner_id: str) -> list[dict]:
        out = []
        for nb in self.store.list_notebooks(learner_id):
            v = self.notebook_view(nb["id"])
            out.append({"id": nb["id"], "cover": cover_image(nb["id"]), "title": v["title"], "status": v["status"], "readiness": v["readiness"],
                        "progress": v["progress"], "last_opened": nb["last_opened"], "chapters": len(v["chapters"]),
                        "open_ready": v["open_ready"], "error": v["error"]})
        return out

    def chapter_view(self, cid: int, open_it: bool = True) -> dict:
        ch = self._chapter(cid)
        nb = self._nb(ch["notebook_id"])
        blocks = self.store.blocks(cid)
        if open_it:
            self.store.update_notebook(nb["id"], last_opened=time.time())
        out_blocks = []
        for b in blocks:
            content = b["content"]
            if b["type"] == "quiz":  # answer keys and hints stay on the server until the student answers
                content = {"questions": [{"id": q["id"], "text": q["text"], "options": q["options"], "concept": q["concept"]}
                                         for q in content["questions"]]}
            if b["type"] == "visual" and (content.get("payload") or {}).get("spec"):   # draw with the current renderer (theme tokens)
                try:
                    from .visuals import render, specs
                    content = {**content, "payload": {**content["payload"], "svg": render.render(specs.parse_spec(content["payload"]["spec"])).svg}}
                except Exception as e:  # noqa: BLE001  keep the stored drawing if it can't be redrawn
                    log.warning("[aasaan] could not redraw visual block %s: %s", b["id"], e)
            out_blocks.append({"id": b["id"], "type": b["type"], "content": content, "verified": b["verified"], "meta": b["meta"]})
        meta = ch["meta"]
        return {"id": cid, "notebook_id": nb["id"], "title": ch["title"], "summary": ch["summary"], "objectives": ch["objectives"],
                "status": ch["status"], "state": self._state(ch), "learning": self._learning(ch), "generation": self._generation(ch),
                "kind": ch["kind"], "error": ch["error"], "review_issues": meta.get("review_issues", []),
                "blocks": out_blocks, "unverified": bool(meta.get("unverified")), "grounded": meta.get("grounded", True), "claims": meta.get("claims"),
                "patched_blocks": meta.get("patched_blocks", []),
                "curator_notes": meta.get("curator_notes", []), "completed": ch["completed"]}

    def open_chapter(self, cid: int) -> None:
        ch = self._chapter(cid)
        if ch["status"] == "ready" and not ch["started"]:
            self.store.update_chapter(cid, started=True)

    def edit_block(self, bid: int, content: dict) -> None:
        b = self.store.get_block(bid)
        if not b:
            raise UserError("unknown block")
        if b["type"] != "notes":
            raise UserError("only notes can be edited")
        md = str(content.get("markdown", ""))
        if len(md) > 20000:
            raise UserError("Notes are too long.")
        self.store.update_block_content(bid, {"markdown": md})

    # ============================================================ session/memory
    def open_session(self, learner_id: str) -> str:
        return self.store.open_session(learner_id)

    def companion_open(self, learner_id: str, nid: str) -> dict:
        nb = self._nb(nid)
        prof = self.profile(learner_id, nb)
        chs = self.store.chapters(nid)
        nxt = next((str(c["id"]) for c in chs if c["status"] == "ready" and not c["completed"]), None)
        # The greeting is fixed text chosen by code from stored memory. A model never writes it, so it can never claim a history the student
        # does not have ("welcome back" on a first session). Opening the app opens a session each time, so "returning" means real history.
        first = not self.store.has_history(learner_id)
        review = {} if first else self.review_summary(learner_id, nid)
        msg = self.greeting(first, nb["title"], prof.mistake_labels, self.store.last_summary(learner_id))
        if review.get("available"):                                  # built from stored mastery and mistakes only, never invented
            msg += f" Want a {review['minutes']}-minute review of {', '.join(review['topics'][:2])}?"
        return {"message": msg, "suggested_next": nxt, "mistake_labels": prof.mistake_labels, "is_first_session": first, "review": review}

    REVIEW_BELOW = 70            # mastery under this percent is worth a review
    REVIEW_MINUTES, REVIEW_QUESTIONS = 5, 5

    def review_items(self, learner_id: str, nid: str) -> list[dict]:
        """A short spaced review: questions on the concepts this learner is weakest at, from this notebook's own quizzes. Lowest mastery first."""
        self._nb(nid)
        mastery = self.store.memory_kind(learner_id, "mastery")
        weak = {c: v["pct"] for c, v in mastery.items() if isinstance(v, dict) and v.get("pct", 100) < self.REVIEW_BELOW}
        labels = {l.lower() for l in self.store.mistake_labels(learner_id)}
        pool = []
        for ch in self.store.chapters(nid):
            qb = next((b for b in self.store.blocks(ch["id"]) if b["type"] == "quiz"), None) if ch["status"] == "ready" else None
            for q in (qb["content"]["questions"] if qb else []):
                key = q.get("objective") if q.get("objective") in weak else q.get("concept")
                if key in weak or str(q.get("concept", "")).lower() in labels:
                    pool.append({**q, "chapter_id": ch["id"], "_rank": weak.get(key, 60)})
        pool.sort(key=lambda q: q["_rank"])
        out = []
        for q in pool[:self.REVIEW_QUESTIONS]:
            q.pop("_rank", None)
            out.append(q)
        return out

    def review_summary(self, learner_id: str, nid: str) -> dict:
        items = self.review_items(learner_id, nid)
        topics = list(dict.fromkeys(q.get("objective") or q.get("concept") for q in items))
        return {"available": bool(items), "minutes": self.REVIEW_MINUTES, "topics": [t for t in topics if t][:3], "count": len(items)}

    @staticmethod
    def greeting(first_session: bool, title: str, labels: list[str], last_summary: Optional[str]) -> str:
        if first_session:
            return f"Your notebook \"{title}\" is taking shape. Open the first chapter whenever you like, and ask me anything in the chat."
        if labels:
            return f"Welcome back. Last time you mixed up '{labels[0]}', so it is worth a quick look. Let's continue with \"{title}\"."
        return f"Welcome back. Let's continue with \"{title}\"."

    def close_session(self, learner_id: str, session_id: str, nid: Optional[str]) -> dict:
        nb = self._nb(nid) if nid else None
        prof = self.profile(learner_id, nb)
        atts = self.store.session_attempts(session_id)
        wrong = sorted({a["misconception"] for a in atts if a["result"] != "correct" and a["misconception"]})
        chs = self.store.chapters(nid) if nid else []
        nxt = next((str(c["id"]) for c in chs if c["status"] == "ready" and not c["completed"]), None)
        last = str(atts[-1]["chapter_id"]) if atts and str(atts[-1]["chapter_id"]) in {str(c["id"]) for c in chs} else None
        res = companion.run(self.router, "close", prof, [str(c["id"]) for c in chs], nxt, session_wrong=wrong, last_chapter=last,
                            notebook_title=nb["title"] if nb else "")
        out = res.value
        existing = set(self.store.mistake_labels(learner_id))
        for label in out.memory_updates.mistake_labels_add:
            if label not in existing:  # Diagnoser already counted this session's mistakes; avoid double counting
                self.store.add_mistake(learner_id, label)
        if out.memory_updates.last_chapter:
            self.store.set_memory(learner_id, "last", "chapter", out.memory_updates.last_chapter)
        if out.memory_updates.notes:
            self.store.set_memory(learner_id, "note", "companion", out.memory_updates.notes)
        self.store.close_session(session_id, out.message)
        return {"message": out.message, "suggested_next": out.suggested_next, "revisit": wrong}

    def scope_view(self, nid: str) -> dict:
        nb = self._nb(nid)
        return {"depth": depth_mod.name_for(self.store, nid), "level": nb["level"] or "beginner", "language": nb["language"],
                "track": (self.store.cache_get(f"scope:{nid}") or {}).get("track", "general"), "depths": list(depth_mod.PROFILES), "levels": ["beginner", "intermediate", "advanced"]}

    def propose_scope(self, nid: str, depth: Optional[str] = None, level: Optional[str] = None) -> Optional[dict]:
        return edits.propose_scope(self, nid, depth, level)

    # ================================================================== quiz
    def answer(self, learner_id: str, session_id: str, cid: int, question_id: str, student_answer: str, attempt: int = 1) -> dict:
        ch = self._chapter(cid)
        qblock = next((b for b in self.store.blocks(cid) if b["type"] == "quiz"), None)
        if not qblock:
            raise UserError("this chapter has no quiz yet")
        raw = next((q for q in qblock["content"]["questions"] if q["id"] == question_id), None)
        if not raw:
            raise UserError("unknown question")
        q = Question.model_validate(raw)
        known = self.store.mistake_labels(learner_id)
        d = diagnoser.run(self.router, q, student_answer, known).value
        if d.result != "correct" and d.confidence < diagnoser.LOW_CONFIDENCE and attempt == 1:
            # unsure: ask for another try instead of marking the student wrong
            return {"question_id": question_id, "follow_up": True, "result": None, "hint": d.hint or q.hint, "misconception": None}
        self.store.add_attempt(learner_id=learner_id, notebook_id=ch["notebook_id"], chapter_id=cid, session_id=session_id,
                               question_id=question_id, concept=q.concept, answer=student_answer, result=d.result,
                               misconception=d.misconception, confidence=d.confidence)
        self.store.update_chapter(cid, started=True)
        if d.result != "correct" and d.misconception:
            self.store.add_mistake(learner_id, d.misconception)
        out = {"question_id": question_id, "follow_up": False, "result": d.result, "misconception": d.misconception,
               "hint": d.hint or q.hint, "confidence": d.confidence}
        if d.result == "correct" or attempt >= 2:  # hint first; reveal the answer only after a second try
            out["correct_answer"] = correct_text(q)
            out["explanation"] = q.explanation
        return out

    def finish_chapter(self, learner_id: str, session_id: str, cid: int) -> dict:
        """Mark the chapter done, then let the Rewriter adjust the course (at most 2 review items per session)."""
        ch = self._chapter(cid)
        nid = ch["notebook_id"]
        rows = self.store.attempts(cid, session_id)
        first: dict[str, dict] = {}
        missed: dict[str, dict] = {}
        for r in rows:  # a question is a gap if ANY attempt was wrong: right-after-a-hint still shows a misunderstanding
            first.setdefault(r["question_id"], r)
            if r["result"] != "correct":
                missed.setdefault(r["question_id"], r)
        total = len(first)
        correct = sum(1 for r in first.values() if r["result"] == "correct")
        agg: dict[str, dict] = {}
        for r in missed.values():
            label = r["misconception"] or r["concept"]
            e = agg.setdefault(label, {"concept": r["concept"], "misconception": label, "count": 0})
            e["count"] += 1
        wrong = sorted(agg.values(), key=lambda e: -e["count"])

        self.store.update_chapter(cid, completed=True, started=True, needs_review=bool(wrong))
        if not wrong and ch["kind"] == "review" and ch["prereqs"]:  # a passed review clears its parent's flag
            self.store.update_chapter(ch["prereqs"][0], needs_review=False)

        added, proposed, reason = [], [], "Course unchanged."
        if wrong and ch["kind"] != "review":
            made = [c for c in self.store.chapters(nid) if c["kind"] == "review" and c["meta"].get("session") == session_id]
            left = MAX_INSERTS_PER_SESSION - len(made)
            if left > 0:
                chs = [{"id": c["id"], "title": c["title"], "position": c["position"], "kind": c["kind"]} for c in self.store.chapters(nid)]
                rw = rewriter.run(self.router, chs, cid, wrong, self.store.mistake_counts(learner_id), left).value
                reason = rw.reason or reason
                inserts = [ins.model_dump() for ins in rw.patch.insert[:left]]
                prop = edits.propose_review(self, nid, session_id, cid, inserts, wrong[0]["misconception"]) if inserts else None
                if prop:                                    # the Rewriter PROPOSES: the course changes only when the learner applies the card
                    proposed.append({"id": prop["id"], "summary": prop["summary"]})
                    reason = "I suggested a short review. Apply it from the chat when you like."
        self._refresh_status(nid)
        qblock = next((b for b in self.store.blocks(cid) if b["type"] == "quiz"), None)
        marks = quiz_grading.mark(rows, qblock["content"]["questions"]) if qblock else None
        if marks:
            for row in marks["by_concept"]:                              # mastery per concept: a running average the Diagnoser and Rewriter can read
                old = self.store.get_memory(learner_id, "mastery", row["concept"], {"pct": row["percent"], "n": 0})
                n = int(old.get("n", 0)) + 1
                self.store.set_memory(learner_id, "mastery", row["concept"], {"pct": round((old["pct"] * (n - 1) + row["percent"]) / n), "n": n})
            if marks["passed"] and not wrong:
                self.store.update_chapter(cid, completed=True)
        return {"score": {"correct": correct, "total": total}, "marks": marks, "added": added, "proposed": proposed, "reason": reason,
                "revisit": [w["misconception"] for w in wrong], "notebook": self.notebook_view(nid)}

    # ================================================================ doubts
    def doubt(self, learner_id: str, cid: int, text: Optional[str], audio: Optional[bytes]) -> dict:
        ch = self._chapter(cid)
        nb = self._nb(ch["notebook_id"])
        prof = self.profile(learner_id, nb)
        if not text and audio:
            text = self.voice.stt(audio, prof.language)
            if not text:
                return {"needs_text": True, "message": "Voice is unavailable right now. Please type your doubt."}
        if not text or not text.strip():
            raise UserError("empty question")
        pool = self._chapter_chunks(ch, self._all_chunks(nb["id"])) + (ch["meta"].get("page_chunks") or [])
        words = tokens(text)
        pool.sort(key=lambda c: -overlap(words, c["text"]))
        notes = next((b["content"].get("markdown", "") for b in self.store.blocks(cid) if b["type"] == "notes"), "")
        res = doubt.run(self.router, text, pool[:6], prof, notes)
        audio_out = self.voice.tts(res.value.answer, prof.language)
        return {"question": text, "answer": res.value.answer, "source_refs": res.value.source_refs,
                "audio_b64": base64.b64encode(audio_out).decode() if audio_out else None}

    # ================================================================= voice
    def transcribe(self, audio: bytes, language: str) -> dict:
        text = self.voice.stt(audio, language)
        if not text:
            return {"text": None, "message": "Voice is unavailable right now. Please type instead."}
        return {"text": text}

    def speak(self, text: str, language: str) -> dict:
        audio = self.voice.tts(text, language)
        return {"audio_b64": base64.b64encode(audio).decode() if audio else None}
