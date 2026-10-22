"""Slides: written after the notes, rules enforced in code, never repeating the notes, never blocking the chapter."""
from app.agents import slides
from app.schemas import ChapterContent, LearnerProfile

NOTES = ("An array stores items in contiguous memory so the item at index i is found in constant time. "
         "Insertion in the middle is slow because later items must be shifted one place to the right. ") * 3


def content():
    return ChapterContent.model_validate({"notes_md": NOTES, "slides": [{"title": f"s{i}", "points": ["a", "b"]} for i in range(4)], "summary": "s", "key_points": ["k"]})


def test_cards_obey_the_rules_and_notes_copies_are_dropped():
    cards = [slides.Card(type="concept", title="Copy", points=["An array stores items in contiguous memory so the item at index i is found in constant time"]),
             slides.Card(type="example", title="Own words", points=[" ".join(["word"] * 30), "b", "c", "d", "e", "f"], note="n" * 500)]
    out, why = slides.tidy(cards, NOTES)
    assert [c["title"] for c in out] == ["Own words"] and "repeats the notes" in why[0]
    assert len(out[0]["points"]) == 4 and all(len(p.split()) <= 12 for p in out[0]["points"]) and len(out[0]["note"]) == 400
    assert slides.similarity("a b", NOTES) == 0 and slides.similarity(NOTES[:200], NOTES) > 0.9


def test_the_deck_is_written_from_the_final_notes_with_the_profile_size(engine):
    res = slides.run(engine.router, {"title": "Arrays", "objectives": ["index"]}, content(), LearnerProfile(learner_id="L1"), {"slides": 10})
    cards = res.value.slides
    assert len(cards) >= 7 and cards[0].type == "title" and cards[-1].type == "recap" and any(c.type in slides.ADDED_VALUE for c in cards)


def test_a_built_chapter_carries_the_typed_deck_and_a_dead_model_keeps_the_teachers_slides(engine, worker, store, monkeypatch):
    nid = engine.create_notebook("L1", "Teach me binary search", "en", [])
    worker.run_until_idle()
    cards = next(b for b in store.blocks(store.chapters(nid)[0]["id"]) if b["type"] == "slides")["content"]["cards"]
    assert len(cards) >= 7 and cards[0]["type"] == "title"

    def boom(*a, **k):
        raise RuntimeError("model is down")
    monkeypatch.setattr(slides, "run", boom)
    nid2 = engine.create_notebook("L2", "Teach me photosynthesis", "en", [])
    worker.run_until_idle()
    old = next(b for b in store.blocks(store.chapters(nid2)[0]["id"]) if b["type"] == "slides")["content"]["cards"]
    assert old and "type" not in old[0] and store.chapters(nid2)[0]["status"] == "ready"        # the chapter still shipped, with the Teacher's slides
