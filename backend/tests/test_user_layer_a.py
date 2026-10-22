"""Doubt answerer (guide mode, exact source quote) and the Companion's review offer."""
from app.agents import doubt
from app.schemas import DoubtOut, LearnerProfile


def test_a_quote_must_really_be_in_a_source_or_it_is_dropped(engine):
    chunks = [{"chunk_id": "s1-p1-c1", "page": 1, "text": "Binary search halves the range at each step. It needs sorted data."}]
    seen = []

    def fake_call(role, prompt, ctx, validate, **kw):
        seen.append(prompt)
        from app.router import RouterResult
        good = validate({"answer": "It halves the range [1].", "source_refs": ["s1-p1-c1"], "quote": "Binary search halves the range at each step."})
        bad = validate({"answer": "x", "source_refs": [], "quote": "Binary search was invented in 1946."})
        return RouterResult((good, bad), "fake", 0)
    engine.router.call = fake_call
    good, bad = doubt.run(engine.router, "what does it do?", chunks, LearnerProfile(learner_id="L1"), guide=True).value
    assert good.quote.startswith("Binary search halves") and bad.quote == ""
    assert "GUIDE MODE" in seen[0] and "do NOT give the final answer" in seen[0]
    doubt.run(engine.router, "q", chunks, LearnerProfile(learner_id="L1"))                                       # the default is to answer directly
    assert "GUIDE MODE" not in seen[1] and "Answer directly" in seen[1]


def test_the_chat_passes_guide_mode_and_shows_the_exact_source_sentence(engine, worker, monkeypatch):
    nid = engine.create_notebook("L1", "Teach me binary search", "en", [])
    worker.run_until_idle()
    got = {}

    def fake_run(router, q, chunks, prof, notes="", style="", history=None, external=False, guide=False):
        from types import SimpleNamespace
        got["guide"] = guide
        return SimpleNamespace(value=DoubtOut(answer="Think about the middle item.", source_refs=[], quote="An exact sentence."))
    monkeypatch.setattr(doubt, "run", fake_run)
    out = engine.handle_chat(nid, "L1", "why is binary search fast?", None, None, "guide")
    assert got["guide"] is True and out["text"].endswith("From the source: “An exact sentence.”")
    engine.handle_chat(nid, "L1", "why is binary search fast?")
    assert got["guide"] is False


def test_the_review_is_built_from_weak_concepts_and_offered_only_to_returning_learners(engine, worker, store):
    nid = engine.create_notebook("L1", "Teach me binary search", "en", [])
    worker.run_until_idle()
    qs = next(b for b in store.blocks(store.chapters(nid)[0]["id"]) if b["type"] == "quiz")["content"]["questions"]
    assert engine.review_items("L1", nid) == [] and engine.companion_open("L1", nid)["review"] == {}                 # first session: nothing is invented
    store.set_memory("L1", "mastery", qs[0]["concept"], {"pct": 40, "n": 2})
    store.set_memory("L1", "mastery", "something strong", {"pct": 95, "n": 3})
    sid = store.open_session("L1")
    store.close_session(sid, "done")                                                                                 # real history now exists
    items = engine.review_items("L1", nid)
    assert items and all(q["concept"] == qs[0]["concept"] and q["chapter_id"] for q in items)
    c = engine.companion_open("L1", nid)
    assert c["review"]["available"] and c["review"]["minutes"] == 5 and "5-minute review" in c["message"]
