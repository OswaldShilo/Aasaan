"""Depth profiles, typed Teacher sections rendered by code, and the deterministic readability clean-up."""
import pytest

from app import depth, readability
from app.schemas import ChapterContent

SLIDES = [{"title": f"s{i}", "points": ["a", "b"]} for i in range(3)]


def test_typed_sections_are_rendered_by_code_in_a_fixed_order():
    secs = [{"kind": "recap", "body": "We covered arrays."}, {"kind": "key_terms", "body": "array: items in order\nindex: a position"},
            {"kind": "explanation", "body": "An array stores items in contiguous memory so any index is found in constant time. " * 2},
            {"kind": "bogus", "body": "dropped"}]
    md = readability.render_sections(secs)
    assert [l for l in md.splitlines() if l.startswith("## ")] == ["## Key terms", "## Explanation", "## Recap"]       # order is code's, not the model's
    assert "- array: items in order" in md and "bogus" not in md
    c = ChapterContent.model_validate({"sections": [s for s in secs if s["kind"] != "bogus"], "slides": SLIDES, "summary": "s", "common_mistakes": ["off by one"]})
    assert c.notes_md.startswith("## Key terms") and c.common_mistakes == ["off by one"]
    with pytest.raises(ValueError, match="explanation"):
        ChapterContent.model_validate({"sections": [{"kind": "recap", "body": "x" * 200}], "slides": SLIDES, "summary": "s"})
    assert ChapterContent.model_validate({"notes_md": "n" * 130, "slides": SLIDES, "summary": "s"}).sections == []          # the old shape still works


def test_clean_notes_repairs_what_models_get_wrong():
    long = " ".join(f"Sentence number {i} says something useful about arrays." for i in range(30))
    md = "## Arrays\n\n" + long + "\n\n| a | b | |---|---| | 1 | 2 | | 3 | 4 |\n\n" + " ".join(["word"] * 50) + " " + "and so on. " * 3
    out, meta = readability.clean_notes(md, "Arrays")
    assert not out.startswith("## Arrays")                                            # the page already shows the title
    assert max(len(p.split()) for p in out.split("\n\n") if not p.startswith("|")) <= 110
    assert "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |" in out
    assert meta["words"] > 150 and meta["reading_minutes"] > 0 and meta["longest_paragraph_words"] <= 110
    code = "```python\n" + "x = 1\n" * 3 + "```"
    assert code in readability.clean_notes(code, "T")[0]                              # code is never reflowed


def test_depth_profiles_and_expansion_rule(store):
    assert depth.PROFILES["standard"] == {"words": 1000, "examples": 2, "slides": 10, "quiz": 8} and depth.name_for(store, "nb") == "standard"
    depth.set_depth(store, "nb", "deep")
    assert depth.profile(store, "nb")["words"] == 1800 and depth.name_for(store, "other") == "standard"
    assert depth.needs_expansion("word " * 500, depth.profile(store, "other")) and not depth.needs_expansion("word " * 700, depth.profile(store, "other"))
    with pytest.raises(ValueError):
        depth.set_depth(store, "nb", "enormous")


def test_a_built_chapter_records_readability_and_depth(engine, worker, store):
    nid = engine.create_notebook("L1", "Teach me binary search", "en", [])
    worker.run_until_idle()
    meta = store.chapters(nid)[0]["meta"]
    assert meta["depth"] == "standard" and meta["readability"]["words"] > 0 and isinstance(meta["common_mistakes"], list)
