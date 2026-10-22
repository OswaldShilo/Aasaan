"""Scoper proposals (depth/level with a rewrite list), Rewriter as a proposal, Practice Curator."""
from conftest import FakeResp

from app import depth
from app.agents import practice

CF = {"status": "OK", "result": {
    "problems": [{"contestId": 1, "index": "A", "name": "Easy search", "rating": 900, "tags": ["binary search"]},
                 {"contestId": 2, "index": "B", "name": "Hard search", "rating": 2400, "tags": ["binary search"]},
                 {"contestId": 3, "index": "C", "name": "Popular search", "rating": 1000, "tags": ["binary search", "math"]},
                 {"contestId": 4, "index": "D", "name": "Unrated", "tags": ["binary search"]}],
    "problemStatistics": [{"contestId": 1, "index": "A", "solvedCount": 10}, {"contestId": 3, "index": "C", "solvedCount": 500}, {"contestId": 2, "index": "B", "solvedCount": 5}]}}


def test_a_scope_change_is_a_proposal_that_lists_what_would_be_rewritten(engine, worker, store):
    nid = engine.create_notebook("L1", "Teach me binary search", "en", [])
    worker.run_until_idle()
    n_ready = len([c for c in store.chapters(nid) if c["status"] == "ready"])
    assert engine.propose_scope(nid, "standard", None) is None                                          # already the setting: nothing to propose
    p = engine.propose_scope(nid, "deep", "advanced")
    assert p["status"] == "pending" and depth.name_for(store, nid) == "standard"                        # nothing changed yet
    assert f"{n_ready} chapters you have not started would be rewritten" in p["lines"][0] and "depth to deep and level to advanced" in p["lines"][0]
    engine.apply_proposal(nid, p["id"])
    assert depth.name_for(store, nid) == "deep" and engine._nb(nid)["level"] == "advanced"
    assert all(c["status"] == "queued" for c in store.chapters(nid) if c["id"] in store.get_proposal(p["id"])["undo"]["regenerated"])
    worker.run_until_idle()
    assert all(c["status"] == "ready" for c in store.chapters(nid)) and store.chapters(nid)[0]["meta"]["depth"] == "deep"
    engine.undo_proposal(nid, p["id"])
    assert depth.name_for(store, nid) == "standard" and engine._nb(nid)["level"] != "advanced"


def test_started_chapters_are_not_rewritten_by_a_scope_change(engine, worker, store):
    nid = engine.create_notebook("L1", "Teach me binary search", "en", [])
    worker.run_until_idle()
    first = store.chapters(nid)[0]["id"]
    store.update_chapter(first, started=True)
    p = engine.propose_scope(nid, "quick", None)
    assert first not in [o for o in store.get_proposal(p["id"])["ops"][0]["affected_ids"]]


def test_the_scope_endpoints_validate_input(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from fake_provider import FakeProvider
    with TestClient(create_app(db_path=str(tmp_path / "t.db"), provider_override=FakeProvider(), start_worker=False, upload_dir=str(tmp_path / "up"))) as c:
        assert c.post("/api/notebooks/x/scope", data={"depth": "enormous"}).status_code == 400


def test_codeforces_practice_is_filtered_by_level_and_never_includes_statements(fake_web):
    fake_web.get(lambda url, p: FakeResp(200, data=CF) if "codeforces.com/api/problemset.problems" in url else None)
    items = practice.codeforces("Binary search basics", "beginner")
    assert [i["title"] for i in items] == ["Popular search", "Easy search"]                           # in the rating band, most solved first, unrated dropped
    assert items[0]["url"] == "https://codeforces.com/problemset/problem/3/C" and "Rating 1000" in items[0]["detail"]
    assert practice.codeforces("Poetry", "beginner") == [] and practice.tag_for("Dynamic programming intro") == "dp"
    blk = practice.practice_for("dsa", "Binary search basics", "beginner")
    assert blk["source"] == "Codeforces" and "never copies" in blk["note"] and practice.practice_for("web", "x", "beginner") is None


def test_exam_practice_uses_only_official_sources_that_answer(fake_web):
    fake_web.get(lambda url, p: FakeResp(200, "<html><title>NTA</title>" + "official page " * 30 + "</html>", headers={"content-type": "text/html"}) if "nta.nic.in" in url else None)
    items = practice.official("Prepare me for NEET physics")
    assert [i["url"] for i in items] == ["https://neet.nta.nic.in/"] and items[0]["detail"] == "Official source"
    assert practice.official("Teach me guitar") == []


def test_a_dsa_chapter_gets_a_practice_block_and_a_failed_api_leaves_none(engine, worker, store, fake_web, monkeypatch):
    fake_web.get(lambda url, p: FakeResp(200, data=CF) if "codeforces.com/api/problemset.problems" in url else None)
    monkeypatch.setattr(engine.store, "cache_get", lambda k, _o=engine.store.cache_get: {"track": "dsa", "level": "beginner"} if k.startswith("scope:") else _o(k))
    nid = engine.create_notebook("L1", "Teach me binary search", "en", [])
    worker.run_until_idle()
    blocks = store.blocks(store.chapters(nid)[0]["id"])
    pb = next((b for b in blocks if b["type"] == "practice"), None)
    assert pb and pb["content"]["items"] and engine.resources_view(nid) is not None
