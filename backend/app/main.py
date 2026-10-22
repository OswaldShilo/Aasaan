"""Aasaan API + static frontend.

Run from the repo root:  python -m uvicorn app.main:create_app --factory --app-dir backend
Needs GEMINI_API_KEY (or OPENROUTER_API_KEY). Without a key the app starts, says so everywhere, and refuses to build notebooks: there is no demo mode.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.gzip import GZipMiddleware

from .backup_loop import run_backup_once
from .security_headers import SecurityHeadersMiddleware
from .sse import StreamHub
from .client_ip import get_client_ip, get_rate_limit_key

from .db import Store
from .engine import Engine, UserError
from .resilience import mask_key
from .router import ModelRouter
from .settings import ROOT, get_db_path, get_uploads_dir, validate_and_prepare_paths
from .tools import ToolRunner
from . import visuals, web
from .visuals.cover import cover_svg
from .worker import Worker

FRONTEND = ROOT / "frontend"


def load_env(path: Path = ROOT / ".env") -> None:
    """Tiny .env reader (KEY=value per line) so keys can be added without extra dependencies."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if v.strip():
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ---- request bodies (module level so FastAPI can resolve annotations)
class LearnerIn(BaseModel):
    pass


class SessionIn(BaseModel):
    learner_id: str


class AnswerIn(BaseModel):
    learner_id: str
    session_id: str
    question_id: str
    answer: str
    attempt: int = 1


class FinishIn(BaseModel):
    learner_id: str
    session_id: str


class CloseIn(BaseModel):
    learner_id: str
    session_id: str
    notebook_id: Optional[str] = None


class BlockIn(BaseModel):
    markdown: str


class TtsIn(BaseModel):
    text: str
    language: str = "en"


class KillIn(BaseModel):
    model: Optional[str] = None


class ChaosIn(BaseModel):
    spec: str = ""


def create_app(db_path: Optional[str] = None, provider_override=None, self_test_on_start: Optional[bool] = None,
               start_worker: bool = True, upload_dir: Optional[str] = None) -> FastAPI:
    load_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    resolved_db = get_db_path(db_path)
    resolved_uploads = get_uploads_dir(upload_dir)
    validate_and_prepare_paths(resolved_db, resolved_uploads)

    store = Store(resolved_db)
    router = ModelRouter(store=store, provider_override=provider_override)
    tools = ToolRunner(store, router.sink, router.chaos)
    visuals.register(tools)
    engine = Engine(store, router, upload_dir=resolved_uploads, tools=tools)
    worker = Worker(engine)
    nb_per_hour = int(os.environ.get("AASAAN_NB_PER_HOUR", "12"))
    creations: dict[str, list[float]] = {}
    backup_per_hour = int(os.environ.get("AASAAN_BACKUP_PER_HOUR", "6"))
    backup_requests: dict[str, list[float]] = {}
    log_ = logging.getLogger("aasaan")

    sse_hub = StreamHub()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if not router.model_ready():
            log_.error("[aasaan] NO AI MODEL IS CONNECTED: set GEMINI_API_KEY (or OPENROUTER_API_KEY) in .env and restart")
        if self_test_on_start if self_test_on_start is not None else router.mode == "live":
            threading.Thread(target=router.log_self_test, name="aasaan-self-test", daemon=True).start()
        task = asyncio.create_task(worker.run()) if start_worker else None
        yield
        await sse_hub.shutdown()
        worker.stop()
        if task:
            task.cancel()

    app = FastAPI(title="Aasaan", lifespan=lifespan)
    app.state.engine, app.state.router, app.state.store, app.state.worker, app.state.tools = engine, router, store, worker, tools
    app.state.sse_hub = sse_hub
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.add_middleware(SecurityHeadersMiddleware)

    # ============================================================== csp report
    csp_report_lock = threading.Lock()
    csp_report_timestamps: list[float] = []
    CSP_MAX_BODY = 16384

    @app.post("/csp-report", status_code=204)
    @app.post("/api/csp-report", status_code=204)
    async def csp_report_endpoint(request: Request):
        """Receive and log Content Security Policy violation reports."""
        cl = request.headers.get("content-length")
        if cl:
            try:
                if int(cl) > CSP_MAX_BODY:
                    return Response(status_code=204)
            except ValueError:
                return Response(status_code=204)

        body = await request.body()
        if len(body) > CSP_MAX_BODY:
            return Response(status_code=204)

        now = time.time()
        with csp_report_lock:
            cutoff = now - 60.0
            while csp_report_timestamps and csp_report_timestamps[0] < cutoff:
                csp_report_timestamps.pop(0)
            if len(csp_report_timestamps) >= 60:
                return Response(status_code=204)
            csp_report_timestamps.append(now)

        if body:
            try:
                data = json.loads(body.decode("utf-8", errors="replace"))
                rep = data.get("csp-report", data) if isinstance(data, dict) else {}
                if isinstance(rep, dict):
                    def _clean_uri(u: str) -> str:
                        return (u.split("?", 1)[0] if isinstance(u, str) else "")[:120]

                    blocked = _clean_uri(rep.get("blocked-uri", ""))
                    directive = str(rep.get("violated-directive", rep.get("effective-directive", "")))[:60]
                    doc = _clean_uri(rep.get("document-uri", ""))
                    disp = str(rep.get("disposition", "report"))[:20]
                    log_.warning("[csp-report] blocked_uri=%s directive=%s document=%s disposition=%s", blocked, directive, doc, disp)
            except Exception:
                pass

        return Response(status_code=204)

    @app.middleware("http")
    async def no_cache_ui(request, call_next):  # always serve the latest UI files
        resp = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.exception_handler(UserError)
    async def user_error(_, exc: UserError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    def require_admin(request: Request) -> None:
        """Internal endpoints: need AASAAN_ADMIN_TOKEN (header X-Admin-Token) when set, else localhost only."""
        token = os.environ.get("AASAAN_ADMIN_TOKEN")
        if token:
            if request.headers.get("x-admin-token") != token:
                raise HTTPException(403, "admin token required")
        elif get_client_ip(request) not in ("127.0.0.1", "::1", "testclient"):
            raise HTTPException(403, "internal endpoint: localhost only (set AASAAN_ADMIN_TOKEN to allow remote use)")

    # ================================================================ health
    @app.get("/health")
    def health():
        """Fast, dependency-free liveness probe: returns 200 with ok: true without touching DB or models."""
        return {"ok": True}

    @app.get("/health/ready")
    @app.get("/api/health/ready")
    def health_ready():
        """Readiness probe: confirms database is writable. Returns 200 on success or 503 on failure."""
        writable, msg = store.check_writable()
        if not writable:
            return JSONResponse({"ok": False, "reason": msg}, status_code=503)
        return {"ok": True}

    @app.get("/api/health")
    def api_health():
        """Detailed application status and model connectivity."""
        return {"ok": True, "app": "aasaan", "mode": router.mode, "model_ready": router.model_ready(), "killed": sorted(router.killed)}

    @app.get("/api/health/models")
    def health_models(refresh: bool = False):
        """Calls every configured model once; ok / failed (with the error) per model. Cached for 60 s."""
        return router.self_test(force=refresh)

    @app.get("/api/health/tools")
    def health_tools():
        """State of every tool: ok, degraded, open_circuit or disabled, with last error, last success and masked keys."""
        decision = {}
        for name, mc in router.cfg["models"].items():
            if mc.get("kind") != "decision":
                continue
            env = router.cfg["providers"].get(mc["provider"], {}).get("api_key_env")
            left = router.circuit_open(name)
            decision[name] = {"state": "disabled" if (env and not os.environ.get(env)) else ("open_circuit" if left > 0 else "ok"),
                              "model_id": mc["id"], "keys": [mask_key(env)] if env else [], "chaos": router.chaos.mode_for("model", name),
                              "killed": name in router.killed}
        return {"tools": tools.health(), "decision_models": decision, "chaos": router.chaos.describe(), "mode": router.mode}

    # ============================================================== learners
    @app.post("/api/learners")
    def create_learner(_: LearnerIn = LearnerIn()):
        import uuid
        return {"learner_id": str(uuid.uuid4())}

    @app.post("/api/session/open")
    def session_open(body: SessionIn):
        return {"session_id": engine.open_session(body.learner_id)}

    @app.post("/api/session/close")
    def session_close(body: CloseIn):
        return engine.close_session(body.learner_id, body.session_id, body.notebook_id)

    # ============================================================= notebooks
    @app.get("/api/notebooks")
    def notebooks(learner_id: str):
        return engine.list_notebooks(learner_id)

    @app.post("/api/notebooks")
    async def create_notebook(
        request: Request,
        learner_id: str = Form(...),
        text: str = Form(""),
        language: str = Form("en"),
        files: list[UploadFile] = File(default=[]),
    ):
        rate_key = get_rate_limit_key(request)
        now = time.time()
        recent = [t for t in creations.get(rate_key, []) if now - t < 3600]
        if len(recent) >= nb_per_hour:
            raise HTTPException(429, "You have created a lot of notebooks this hour. Please try again later.")
        data = [(f.filename or "file", f.content_type or "", await f.read()) for f in files if f is not None and f.filename]
        nid = await run_in_threadpool(engine.create_notebook, learner_id, text, language, data)
        creations[rate_key] = recent + [now]
        return {"notebook_id": nid}

    @app.get("/api/notebooks/{nid}/cover.svg")
    def notebook_cover(nid: str):
        """Generated cover (deterministic from the title): no network, no model."""
        nb = engine.notebook_view(nid)
        return Response(cover_svg(nb["title"], nb.get("goal") or ""), media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/api/notebooks/{nid}")
    def notebook(nid: str):
        return engine.notebook_view(nid)

    @app.delete("/api/notebooks/{nid}")
    async def delete_notebook(nid: str):
        engine.delete_notebook(nid)
        await sse_hub.remove_notebook(nid)
        return {"ok": True}

    @app.post("/api/notebooks/{nid}/retry")
    def retry_notebook(nid: str):
        engine.retry_notebook(nid)
        return {"ok": True}

    @app.get("/api/notebooks/{nid}/stream")
    async def stream(nid: str, request: Request, last_event_id: Optional[int] = Query(None)):
        """Server-sent events: live notebook snapshot with monotonic IDs, keepalives, and auto-reconnect."""
        engine._nb(nid)
        return StreamingResponse(
            sse_hub.subscribe(nid, engine, request, last_event_id=last_event_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/dev/trace/{nid}")
    def dev_trace(nid: str):
        """Developer trace (AASAAN_TRACE=on only; never shown to learners): the board mirror, the agent messages, the technical events and every decision record."""
        if os.environ.get("AASAAN_TRACE", "off").strip().lower() not in ("on", "1", "true", "yes"):
            raise HTTPException(404, "not found")
        engine._nb(nid)
        sh = engine._shadow(nid)
        board = sh.trace(nid) if sh else {"artifacts": [], "messages": [], "note": "AASAAN_ORCHESTRATOR is off for this notebook"}
        return {**board, "events": store.events(300), "decisions": engine.decisions.recent(nid, limit=500)}

    @app.get("/api/notebooks/{nid}/scope")
    def scope(nid: str):
        return engine.scope_view(nid)

    @app.post("/api/notebooks/{nid}/scope")
    def set_scope(nid: str, depth: Optional[str] = Form(None), level: Optional[str] = Form(None)):
        """A scope change is a PROPOSAL (what would be rewritten, roughly how long); it is applied from the chat card. None = nothing to change."""
        if depth not in (None, "", "quick", "standard", "deep") or level not in (None, "", "beginner", "intermediate", "advanced"):
            raise HTTPException(400, "unknown depth or level")
        return {"proposal": engine.propose_scope(nid, depth or None, level or None)}

    @app.get("/api/notebooks/{nid}/review")
    def review(nid: str, learner_id: str):
        """A 5-minute spaced review from what this learner got wrong or is weak at (empty when nothing is weak)."""
        return {"summary": engine.review_summary(learner_id, nid), "questions": engine.review_items(learner_id, nid)}

    @app.get("/api/notebooks/{nid}/decisions")
    def decisions(nid: str, since: int = 0, chapter_id: Optional[int] = None, agent: Optional[str] = None, severity: Optional[str] = None):
        """The Activity feed: what each agent decided and what came out of it, in plain words. Replays from `since` (0 = everything)."""
        return engine.decisions_view(nid, since, chapter_id, agent, severity)

    # ---- chat thread
    @app.get("/api/notebooks/{nid}/messages")
    def messages(nid: str, after: int = 0):
        return {"messages": engine.chat_history(nid, after)}

    @app.get("/api/notebooks/{nid}/resources")
    def resources(nid: str):
        return engine.resources_view(nid)

    @app.post("/api/notebooks/{nid}/resources")
    async def add_resources(nid: str, files: list[UploadFile] = File(default=[]), urls: str = Form(""), text: str = Form("")):
        data = [(f.filename or "file", f.content_type or "", await f.read()) for f in files if f is not None and f.filename]
        link_list = [u for u in urls.split() if u.startswith(("http://", "https://"))]
        if not (data or link_list or text.strip()):
            raise HTTPException(400, "Add a file, a link or some text.")
        try:
            out = await run_in_threadpool(engine.add_resources, nid, data, link_list) if (data or link_list) else []
            if text.strip():
                out.append(await run_in_threadpool(engine.add_pasted_text, nid, text.strip()))
            return {"resources": out}
        except UserError as e:
            raise HTTPException(400, str(e))

    def _res_guard(fn, *a):
        try:
            return fn(*a)
        except UserError as e:
            raise HTTPException(404, str(e))

    @app.get("/api/notebooks/{nid}/resources/{sid}/impact")
    def resource_impact(nid: str, sid: str):
        return _res_guard(engine.resource_impact, nid, sid)

    @app.get("/api/notebooks/{nid}/resources/{sid}/preview")
    def resource_preview(nid: str, sid: str):
        return _res_guard(engine.resource_preview, nid, sid)

    @app.get("/api/notebooks/{nid}/resources/{sid}/file")
    def resource_file(nid: str, sid: str):
        p = _res_guard(engine.resource_file, nid, sid)
        return FileResponse(p, headers={"Content-Disposition": "inline", "X-Content-Type-Options": "nosniff"})

    @app.get("/api/notebooks/{nid}/resources/{sid}/thumb")
    def resource_thumb(nid: str, sid: str):
        png = _res_guard(engine.resource_thumb, nid, sid)
        if not png:
            raise HTTPException(404, "no thumbnail")
        return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=600"})

    @app.post("/api/notebooks/{nid}/resources/{sid}/reprocess")
    def resource_reprocess(nid: str, sid: str):
        return _res_guard(engine.reprocess_resource, nid, sid)

    @app.delete("/api/notebooks/{nid}/resources/{sid}")
    def resource_remove(nid: str, sid: str, regenerate: bool = False):
        return _res_guard(engine.remove_resource, nid, sid, regenerate)

    @app.post("/api/notebooks/{nid}/chat")
    async def chat(nid: str, learner_id: str = Form(...), text: str = Form(""), chapter_id: Optional[int] = Form(None), block_ids: str = Form(""),
                   mode: str = Form("answer"), files: list[UploadFile] = File(default=[])):
        """One chat turn as a server-sent stream: `user` (stored), `status`, `delta` pieces of the validated answer, `done` (stored, with chips).
        The answer is validated against the sources BEFORE it is shown, so it streams from the checked text, not from raw model tokens."""
        engine._nb(nid)
        text = (text or "").strip()[:4000]
        data = [(f.filename or "file", f.content_type or "", await f.read()) for f in files if f is not None and f.filename]
        urls = web.extract_urls(text)
        question = web.URL_RE.sub("", text).strip()
        if not (text or data):
            raise HTTPException(400, "Type a message or attach a file.")
        try:
            await run_in_threadpool(engine.validate_resources, nid, data, urls)
        except UserError as e:
            raise HTTPException(400, str(e))
        blocks = [int(x) for x in block_ids.split(",") if x.strip().isdigit()][:3]

        def ev(name: str, payload: dict) -> str:
            return f"event: {name}\ndata: {json.dumps(payload)}\n\n"

        async def gen():
            attach = [{"name": n, "size": len(d), "kind": "pdf" if n.lower().endswith(".pdf") else "image" if c.startswith("image/") else "text"} for n, c, d in data]
            mid = store.add_message(nid, "user", text, kind="chat", attachments=attach + [{"name": u, "kind": "url"} for u in urls])
            yield ev("user", {"id": mid, "content": text, "attachments": attach})
            try:
                if data or urls:
                    await run_in_threadpool(engine.add_resources, nid, data, urls)
                if not question:
                    yield ev("done", {"id": None, "refs": []})
                    return
                yield ev("status", {"text": "Thinking…"})
                res = await run_in_threadpool(engine.handle_chat, nid, learner_id, question, chapter_id, blocks, "guide" if mode == "guide" else "answer")
                if res["type"] == "proposal":      # a card with Apply and Dismiss: nothing has changed yet
                    p = res["proposal"]
                    yield ev("proposal", {"id": p["message_id"], "proposal": {k: v for k, v in p.items() if k != "message_id"}, "content": p["summary"]})
                    yield ev("done", {"id": p["message_id"], "refs": []})
                    return
                res = {"answer": res["text"], "refs": res["refs"]}
            except Exception as e:  # noqa: BLE001  the student gets a calm message, the detail stays in the log
                log_.warning("[aasaan] chat failed: %s", e)
                msg = "I couldn't answer that just now. Please try again in a moment."
                aid = store.add_message(nid, "assistant", msg, kind="chat")
                yield ev("error", {"id": aid, "content": msg})
                return
            aid = store.add_message(nid, "assistant", res["answer"], kind="chat", refs=res["refs"])
            words = re.findall(r"\S+\s*", res["answer"])
            for i in range(0, len(words), 3):
                yield ev("delta", {"text": "".join(words[i:i + 3])})
                await asyncio.sleep(0.03)
            yield ev("done", {"id": aid, "refs": res["refs"]})

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---- proposals: the student applies, dismisses or undoes
    def _prop(fn, nid, pid):
        try:
            return fn(nid, pid)
        except UserError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/notebooks/{nid}/proposals/{pid}")
    def proposal(nid: str, pid: int):
        return _prop(engine.proposal, nid, pid)

    @app.post("/api/notebooks/{nid}/proposals/{pid}/apply")
    async def proposal_apply(nid: str, pid: int):
        try:
            return await run_in_threadpool(engine.apply_proposal, nid, pid)
        except UserError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/notebooks/{nid}/proposals/{pid}/undo")
    async def proposal_undo(nid: str, pid: int):
        try:
            return await run_in_threadpool(engine.undo_proposal, nid, pid)
        except UserError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/notebooks/{nid}/proposals/{pid}/dismiss")
    def proposal_dismiss(nid: str, pid: int):
        return _prop(engine.dismiss_proposal, nid, pid)

    @app.get("/api/notebooks/{nid}/companion")
    def companion_open(nid: str, learner_id: str):
        return engine.companion_open(learner_id, nid)

    # ============================================================== chapters
    @app.get("/api/chapters/{cid}")
    def chapter(cid: int):
        return engine.chapter_view(cid)

    @app.post("/api/chapters/{cid}/open")
    def chapter_open(cid: int):
        engine.open_chapter(cid)
        return {"ok": True}

    @app.post("/api/chapters/{cid}/prioritize")
    def chapter_prioritize(cid: int):
        return engine.prioritize(cid)

    @app.post("/api/chapters/{cid}/retry")
    def chapter_retry(cid: int):
        engine.retry_chapter(cid)
        return {"ok": True}

    @app.post("/api/chapters/{cid}/recheck")
    async def chapter_recheck(cid: int):
        return await run_in_threadpool(engine.recheck_chapter, cid)

    @app.put("/api/blocks/{bid}")
    def edit_block(bid: int, body: BlockIn):
        engine.edit_block(bid, {"markdown": body.markdown})
        return {"ok": True}

    @app.post("/api/chapters/{cid}/answer")
    def answer(cid: int, body: AnswerIn):
        return engine.answer(body.learner_id, body.session_id, cid, body.question_id, body.answer, body.attempt)

    @app.post("/api/chapters/{cid}/finish")
    def finish(cid: int, body: FinishIn):
        return engine.finish_chapter(body.learner_id, body.session_id, cid)

    @app.post("/api/chapters/{cid}/doubt")
    async def doubt(cid: int, learner_id: str = Form(...), text: Optional[str] = Form(None), audio: Optional[UploadFile] = File(None)):
        data = await audio.read() if audio is not None else None
        return await run_in_threadpool(engine.doubt, learner_id, cid, text, data)

    # ================================================================= voice
    @app.post("/api/voice/stt")
    async def stt(language: str = Form("en"), audio: UploadFile = File(...)):
        data = await audio.read()
        if len(data) > 2_000_000:
            raise HTTPException(400, "Recording too long. Keep it under 30 seconds.")
        return await run_in_threadpool(engine.transcribe, data, language)

    @app.post("/api/voice/tts")
    async def tts(body: TtsIn):
        return await run_in_threadpool(engine.speak, body.text, body.language)

    # ======================================== internal / demo helpers (not in the UI)
    @app.get("/api/events")
    def events(request: Request, limit: int = 100):
        require_admin(request)
        return store.events(limit)

    @app.post("/api/admin/kill")
    def kill(request: Request, body: KillIn):
        require_admin(request)
        if not body.model:
            raise HTTPException(400, "model required")
        router.kill(body.model)
        return {"killed": sorted(router.killed)}

    @app.get("/api/admin/chaos")
    def get_chaos(request: Request):
        require_admin(request)
        return {"spec": router.chaos.describe()}

    @app.post("/api/admin/chaos")
    def set_chaos(request: Request, body: ChaosIn):
        """e.g. {"spec": "tool:commons=fail,model:jev=fail"}; empty string clears it."""
        require_admin(request)
        router.chaos.set(body.spec)
        return {"spec": router.chaos.describe()}

    @app.post("/api/admin/revive")
    def revive(request: Request, body: KillIn):
        require_admin(request)
        router.revive(body.model)
        return {"killed": sorted(router.killed)}

    @app.post("/api/admin/backup")
    async def force_backup(request: Request):
        admin_token = os.environ.get("AASAAN_ADMIN_TOKEN", "").strip()
        if not admin_token:
            raise HTTPException(503, "admin token not configured")

        req_token = request.headers.get("x-admin-token", "")
        if not req_token or not hmac.compare_digest(req_token, admin_token):
            raise HTTPException(403, "admin token required")

        rate_key = get_rate_limit_key(request)
        now = time.time()
        recent = [t for t in backup_requests.get(rate_key, []) if now - t < 3600]
        if len(recent) >= backup_per_hour:
            raise HTTPException(429, "Too many backup requests. Please try again later.")
        backup_requests[rate_key] = recent + [now]

        ok, status_msg, final_path, size_bytes, duration = await run_in_threadpool(
            run_backup_once, db_path=resolved_db
        )
        if not ok:
            return JSONResponse(
                {
                    "ok": False,
                    "error": status_msg,
                    "file": final_path.name if final_path else None,
                    "size": size_bytes,
                    "duration": round(duration, 3),
                },
                status_code=500,
            )
        return {
            "ok": True,
            "file": final_path.name if final_path else None,
            "size": size_bytes,
            "duration": round(duration, 3),
        }

    # ================================================================ frontend
    if FRONTEND.exists():
        app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")

        @app.get("/")
        def index():
            return FileResponse(FRONTEND / "index.html")

    return app
