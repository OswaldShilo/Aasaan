"""Critic claim stats: "Verified N of M claims" comes from code counts, not model prose."""
from app.agents import critic, gates
from app.schemas import ChapterContent, LearnerProfile


def content(notes):
    return ChapterContent.model_validate({"notes_md": notes, "slides": [{"title": f"s{i}", "points": ["a", "b"]} for i in range(3)], "summary": "s", "source_refs": []})


NOTES = "## Arrays\n\nAn array stores items in contiguous memory. Access by index takes constant time. Insertion in the middle shifts later items. " * 2


def test_all_supported_gives_counts_and_no_model_call(engine, monkeypatch):
    monkeypatch.setattr(gates, "claim_support", lambda router, notes, chunks: [])
    out = critic.run(engine.router, content(NOTES), [{"chunk_id": "s1-p1-c1", "text": "x", "page": 1}], LearnerProfile(learner_id="L1")).value
    assert out.claims_flagged == 0 and out.claims_total == len(gates.claim_sentences(NOTES)) > 0


def test_flagged_claims_are_counted_when_the_model_reviews(engine, monkeypatch):
    flagged = gates.claim_sentences(NOTES)[:1]
    monkeypatch.setattr(gates, "claim_support", lambda router, notes, chunks: flagged)
    out = critic.run(engine.router, content(NOTES), [{"chunk_id": "s1-p1-c1", "text": "x", "page": 1}], LearnerProfile(learner_id="L1")).value
    assert out.claims_flagged == 1 and out.claims_total == len(gates.claim_sentences(NOTES))


def test_the_chapter_shows_verified_n_of_m(engine, worker, store, monkeypatch):
    monkeypatch.setattr(gates, "claim_support", lambda router, notes, chunks: [])
    nid = engine.create_notebook("L1", "Teach me binary search", "en", [])
    worker.run_until_idle()
    ch = store.chapters(nid)[0]
    cl = ch["meta"]["claims"]
    assert cl and cl["verified"] == cl["total"] > 0
