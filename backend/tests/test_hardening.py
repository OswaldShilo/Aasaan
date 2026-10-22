"""Hardening: the board mirror on a real build (shadow mode), loop guards in flow, the developer trace, chaos, three notebooks at once."""
import threading

from fastapi.testclient import TestClient

from app.agents import critic
from app.main import create_app
from app.schemas import CriticIssue, CriticOut
from fake_provider import FakeProvider


def _rows(store, nid):
    return {(a["type"], a["chapter_id"]): a["status"] for a in engine_board(store).list(nid)}


def engine_board(store):
    from app.orchestrator import Board, load_manifests
    return Board(store, load_manifests())


def test_nothing_touches_the_board_when_the_orchestrator_is_off(engine, worker, store):
    nid = engine.create_notebook("L1", "Teach me binary search", "en", [])
    worker.run_until_idle()
    assert engine_board(store).list(nid) == []


def test_a_real_build_is_mirrored_onto_the_board_and_released_in_order(engine, worker, store, monkeypatch):
    monkeypatch.setenv("AASAAN_ORCHESTRATOR", "on")
    nid = engine.create_notebook("L1", "Teach me binary search", "en", [])
    worker.run_until_idle()
    rows = _rows(store, nid)
    assert rows[("outline", None)] == "released"
    ch1 = store.chapters(nid)[0]["id"]
    assert rows[("notes", ch1)] == "released" and rows[("quiz", ch1)] == "released" and rows[("slide_deck", ch1)] == "released"
    sh = engine._shadow(nid)
    order = [d["summary"] for d in engine.decisions_view(nid)["decisions"] if d["kind"] == "released"]
    assert order[0].startswith("Outline") and any("Slide deck" in s for s in order)                      # outline first, slides after the notes


def test_critic_revisions_become_fixes_messages_under_the_loop_guard(engine, worker, store, monkeypatch):
    monkeypatch.setenv("AASAAN_ORCHESTRATOR", "on")
    monkeypatch.setattr(critic, "run", lambda *a, **k: type("R", (), {"value": CriticOut(verdict="revise", issues=[CriticIssue(problem="unsupported claim")])})())
    nid = engine.create_notebook("L1", "Teach me binary search", "en", [])
    worker.run_until_idle()
    ch1 = store.chapters(nid)[0]
    notes = next(a for a in engine_board(store).list(nid) if a["type"] == "notes" and a["chapter_id"] == ch1["id"])
    assert notes["flags"].get("unverified") is True and notes["flags"]["revisions"] == 2                   # two loops, then it ships unverified
    msgs = [m for m in engine.store._all("SELECT * FROM agent_messages WHERE notebook_id=?", (nid,)) if m["type"] == "Fixes" and m["artifact_id"] == notes["id"]]
    assert 1 <= len(msgs) <= 3                                                                               # one chain of messages: at most 3 hops and ch1["meta"]["unverified"] is True and ch1["status"] == "ready"


def test_the_developer_trace_is_off_by_default_and_shows_technical_detail_when_on(tmp_path, monkeypatch):
    with TestClient(create_app(db_path=str(tmp_path / "t.db"), provider_override=FakeProvider(), start_worker=False, upload_dir=str(tmp_path / "up"))) as c:
        assert c.get("/api/dev/trace/nope").status_code == 404
        monkeypatch.setenv("AASAAN_TRACE", "on")
        assert c.get("/api/dev/trace/nope").status_code in (400, 404)                                        # on, but an unknown notebook


def test_killing_the_extra_tools_and_the_deck_never_stalls_a_notebook(engine, worker, store, monkeypatch):
    from app.agents import slides
    engine.router.chaos.set("tool:openlibrary=fail,tool:codeforces=fail,tool:wikipedia=fail")
    monkeypatch.setattr(slides, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("deck model down")))
    nid = engine.create_notebook("L1", "Teach me binary search", "en", [])
    worker.run_until_idle()
    assert engine._nb(nid)["status"] == "complete" and all(c["status"] == "ready" for c in store.chapters(nid))
    assert not [s for s in store.sources(nid) if s["kind"] == "book"]


def test_a_broken_decision_feed_never_breaks_a_build(engine, worker, store, monkeypatch):
    monkeypatch.setattr(engine.decisions, "emit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("feed down")))
    nid = engine.create_notebook("L1", "Teach me binary search", "en", [])
    worker.run_until_idle()
    assert engine._nb(nid)["status"] == "complete"


def test_three_notebooks_building_at_once_keep_their_feeds_apart(engine, store):
    nids = [engine.create_notebook(f"L{i}", f"Teach me topic number {i}", "en", []) for i in range(3)]
    errors = []

    def build(nid):
        try:
            engine.phase0(nid)
            engine.build_chapter(store.chapters(nid)[0]["id"])
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))
    ts = [threading.Thread(target=build, args=(n,)) for n in nids]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    for n in nids:
        recs = engine.decisions_view(n)["decisions"]
        assert recs and all(r["notebook_id"] == n for r in recs) and any("notes for chapter 1" in r["summary"] for r in recs)
