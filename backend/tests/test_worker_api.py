"""The asyncio worker and the HTTP API (FakeProvider test double)."""
import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.worker import Worker
from fake_provider import FakeProvider


def test_worker_limits_concurrency_and_finishes_everything(engine, store):
    nid = engine.create_notebook("l1", "I want to learn data structures", "en", [])
    engine.process_job(store.pending_jobs()[0]["id"])  # outline + chapter 1
    state = {"active": 0, "peak": 0}
    real_set = store.set_chapter_status

    def slow_build(cid):
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.12)
        real_set(cid, "ready")
        state["active"] -= 1

    engine.build_chapter = slow_build

    async def go():
        w = Worker(engine, concurrency=2, poll_s=0.02)
        task = asyncio.create_task(w.run())
        for _ in range(400):
            if store.active_jobs(nid) == 0:
                break
            await asyncio.sleep(0.05)
        w.stop()
        await asyncio.wait_for(task, 2)

    asyncio.run(go())
    assert state["peak"] == 2  # two chapters at a time, not seven
    assert all(c["status"] == "ready" for c in store.chapters(nid))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("AASAAN_ADMIN_TOKEN", raising=False)
    app = create_app(db_path=str(tmp_path / "t.db"), provider_override=FakeProvider(), upload_dir=str(tmp_path / "up"))
    with TestClient(app) as c:
        yield c


def wait_for(client, nid, key, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        v = client.get(f"/api/notebooks/{nid}").json()
        if v[key]:
            return v
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {key}: {v['status']}")


def test_full_http_flow(client):
    assert client.get("/api/health").json()["app"] == "aasaan"
    lid = client.post("/api/learners").json()["learner_id"]
    sid = client.post("/api/session/open", json={"learner_id": lid}).json()["session_id"]
    nid = client.post("/api/notebooks", data={"learner_id": lid, "text": "I want to learn DSA", "language": "en"}).json()["notebook_id"]
    v = wait_for(client, nid, "open_ready")  # opens as soon as the outline and chapter 1 are ready
    assert 3 <= len(v["chapters"]) <= 20 and v["mode"] == "test"
    assert client.get("/api/notebooks", params={"learner_id": lid}).json()[0]["id"] == nid

    cid = v["chapters"][0]["id"]
    ch = client.get(f"/api/chapters/{cid}").json()
    assert ch["status"] == "ready" and {"notes", "slides", "quiz", "summary"} <= {b["type"] for b in ch["blocks"]}
    notes = next(b for b in ch["blocks"] if b["type"] == "notes")
    assert client.put(f"/api/blocks/{notes['id']}", json={"markdown": "## Mine\n\nEdited"}).status_code == 200
    q = next(b for b in ch["blocks"] if b["type"] == "quiz")["content"]["questions"][0]
    fb = client.post(f"/api/chapters/{cid}/answer", json={"learner_id": lid, "session_id": sid, "question_id": q["id"], "answer": q["options"][-1]}).json()
    assert fb["result"] == "wrong" and fb["misconception"]
    fin = client.post(f"/api/chapters/{cid}/finish", json={"learner_id": lid, "session_id": sid}).json()
    assert len(fin["proposed"]) == 1 and fin["added"] == []
    assert client.post(f"/api/notebooks/{nid}/proposals/{fin['proposed'][0]['id']}/apply").status_code == 200        # the Rewriter proposes; the learner applies

    for _ in range(300):  # everything fills in, including the review chapter
        v = client.get(f"/api/notebooks/{nid}").json()
        if v["status"] == "complete":
            break
        time.sleep(0.1)
    assert v["status"] == "complete" and any(c["kind"] == "review" for c in v["chapters"])
    assert client.get(f"/api/notebooks/{nid}/companion", params={"learner_id": lid}).json()["message"]
    assert client.post("/api/session/close", json={"learner_id": lid, "session_id": sid, "notebook_id": nid}).json()["message"]
    assert client.post(f"/api/chapters/{cid}/doubt", data={"learner_id": lid, "text": "what is this?"}).json()["answer"]
    assert client.post("/api/voice/tts", json={"text": "hi", "language": "hi"}).json() == {"audio_b64": None}
    assert client.delete(f"/api/notebooks/{nid}").status_code == 200


def test_uploads_are_validated(client):
    lid = client.post("/api/learners").json()["learner_id"]
    bad = client.post("/api/notebooks", data={"learner_id": lid, "text": "x"}, files=[("files", ("a.exe", b"1", "application/octet-stream"))])
    assert bad.status_code == 400 and "only photos and PDFs" in bad.json()["error"]
    assert client.post("/api/notebooks", data={"learner_id": lid, "text": ""}).status_code == 400
    ok = client.post("/api/notebooks", data={"learner_id": lid, "text": ""}, files=[("files", ("p.png", b"\x89PNG", "image/png"))])
    assert ok.status_code == 200


def test_per_ip_notebook_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("AASAAN_NB_PER_HOUR", "1")
    with TestClient(create_app(db_path=str(tmp_path / "r.db"), provider_override=FakeProvider(), start_worker=False)) as c:
        lid = c.post("/api/learners").json()["learner_id"]
        assert c.post("/api/notebooks", data={"learner_id": lid, "text": "learn math"}).status_code == 200
        assert c.post("/api/notebooks", data={"learner_id": lid, "text": "learn physics"}).status_code == 429


def test_internal_endpoints_are_protected(tmp_path, monkeypatch):
    monkeypatch.setenv("AASAAN_ADMIN_TOKEN", "s3cret")
    with TestClient(create_app(db_path=str(tmp_path / "a.db"), provider_override=FakeProvider(), start_worker=False)) as c:
        assert c.get("/api/events").status_code == 403
        assert c.post("/api/admin/kill", json={"model": "gemma_31b"}).status_code == 403
        h = {"x-admin-token": "s3cret"}
        assert c.post("/api/admin/kill", json={"model": "gemma_31b"}, headers=h).json()["killed"] == ["gemma_31b"]
        assert c.get("/api/events", headers=h).status_code == 200


def test_health_models_endpoint(client):
    rep = client.get("/api/health/models").json()
    assert rep["mode"] == "test" and rep["models"]["gemma_31b"]["id"] == "gemma-4-31b-it" and client.get("/health").json()["ok"]


def test_new_notebooks_are_not_blocked_by_background_chapters(engine, store):
    """Two slow chapters of an old notebook must not delay the first chapter of a new notebook."""
    old = engine.create_notebook("l1", "I want to learn algebra", "en", [])
    engine.process_job(store.pending_jobs()[0]["id"])
    release = {"go": False}
    started = {"new": None}
    real_build = engine.build_chapter

    def build(cid):
        ch = store.get_chapter(cid)
        if ch["notebook_id"] == old:
            while not release["go"]:
                time.sleep(0.02)  # a slow background chapter
        elif started["new"] is None:
            started["new"] = time.time()
        return real_build(cid)

    engine.build_chapter = build

    async def go():
        w = Worker(engine, concurrency=2, poll_s=0.02)
        task = asyncio.create_task(w.run())
        await asyncio.sleep(0.3)                       # both chapter slots are now busy with the old notebook
        assert len(w.active) == 2
        engine.create_notebook("l1", "I want to learn physics", "en", [])
        for _ in range(300):
            if started["new"]:
                break
            await asyncio.sleep(0.02)
        got = started["new"] is not None               # the new notebook's first chapter started while the old ones are stuck
        release["go"] = True
        for _ in range(500):
            if store.active_jobs() == 0:
                break
            await asyncio.sleep(0.05)
        w.stop()
        await asyncio.wait_for(task, 3)
        return got

    assert asyncio.run(go())


def test_notebook_jobs_sort_before_chapter_jobs(engine, store):
    nid = engine.create_notebook("l1", "I want to learn algebra", "en", [])
    engine.process_job(store.pending_jobs()[0]["id"])
    store.update_chapter(store.chapters(nid)[3]["id"], priority=80)   # an urgent chapter
    engine.create_notebook("l1", "I want to learn chemistry", "en", [])
    assert store.pending_jobs()[0]["kind"] == "notebook"


def test_without_a_model_key_the_app_says_so_and_refuses_to_build(tmp_path):
    """There is no demo mode: no key means a clear message, not placeholder content."""
    with TestClient(create_app(db_path=str(tmp_path / "nokey.db"), start_worker=False)) as c:
        assert c.get("/api/health").json()["model_ready"] is False
        lid = c.post("/api/learners").json()["learner_id"]
        r = c.post("/api/notebooks", data={"learner_id": lid, "text": "Teach me binary search", "language": "en"})
        assert r.status_code == 400 and "GEMINI_API_KEY" in r.text
