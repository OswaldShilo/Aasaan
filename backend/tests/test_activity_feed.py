"""Activity feed backend: milestone decisions, calm fallback records with no model names, replay, snapshot seq."""
from fastapi.testclient import TestClient

from app.main import create_app
from app.tools import current_notebook
from fake_provider import FakeProvider


def test_a_build_leaves_plain_language_decisions_and_the_snapshot_carries_a_seq(engine, worker, store):
    nid = engine.create_notebook("L1", "Teach me binary search", "en", [])
    worker.run_until_idle()
    feed = engine.decisions_view(nid)["decisions"]
    said = [d["summary"] for d in feed]
    assert any(s.startswith("Architect planned") and "chapters" in s for s in said)
    assert any(s.startswith("Teacher accepted notes for chapter 1") for s in said) and any("Critic accepted chapter 1" in s for s in said)
    assert any("Quizzer" in s and "quiz questions" in s for s in said)
    assert engine.notebook_view(nid)["decision_seq"] == feed[-1]["id"]
    later = engine.decisions_view(nid, since=feed[0]["id"])["decisions"]                      # replay from a point: no duplicates
    assert [d["id"] for d in later] == [d["id"] for d in feed[1:]]
    assert all(not d["summary"].lower().count("gemma") and "http" not in d["summary"] for d in feed)


def test_fallbacks_are_calm_and_never_name_a_model(engine, worker, store):
    nid = engine.create_notebook("L1", "Teach me binary search", "en", [])
    worker.run_until_idle()
    tok = current_notebook.set(nid)
    try:
        engine.router.sink.emit("teacher", "gemma_26b", 1, "fallback", error="HTTP 429 quota exceeded for gemma-4-26b with key AIzaSyABCDEFGHIJKLMNOP")
        engine.router.sink.emit("teacher", "gemma_26b", 1, "fallback", error="again")        # the same agent and status within 30 s is one record
    finally:
        current_notebook.reset(tok)
    recs = [d for d in engine.decisions_view(nid)["decisions"] if d["kind"] == "fell_back"]
    assert len(recs) == 1 and recs[0]["severity"] == "notice" and recs[0]["reason_code"] == "rate_limited"
    assert "gemma" not in (recs[0]["summary"] + recs[0]["reason_text"]).lower() and "AIza" not in recs[0]["summary"]


def test_the_decisions_endpoint_serves_the_feed_and_rejects_unknown_notebooks(tmp_path):
    with TestClient(create_app(db_path=str(tmp_path / "t.db"), provider_override=FakeProvider(), start_worker=False, upload_dir=str(tmp_path / "up"))) as c:
        assert c.get("/api/notebooks/nope/decisions").status_code in (400, 404)
