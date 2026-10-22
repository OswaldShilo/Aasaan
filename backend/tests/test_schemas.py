import pytest
from pydantic import ValidationError

from app.schemas import (ChapterContent, CriticOut, OutlineOut, Question, QuizOut, RewriterOut, Slide, is_acyclic, resolve_key)


def chap(key, prereqs=(), title=None):
    return {"key": key, "title": title or f"Chapter {key}", "prereqs": list(prereqs)}


def outline(n=4, **over):
    chapters = [chap(f"c{i}", [f"c{i - 1}"] if i > 1 else []) for i in range(1, n + 1)]
    return {"title": "T", "chapters": chapters, **over}


def test_valid_outline():
    assert len(OutlineOut.model_validate(outline(8)).chapters) == 8


def test_outline_size_limits():
    with pytest.raises(ValidationError, match="3-20"):
        OutlineOut.model_validate(outline(2))
    with pytest.raises(ValidationError, match="3-20"):
        OutlineOut.model_validate(outline(21))


def test_outline_cycle_self_loop_unknown_prereq_duplicates():
    cyc = outline(3)
    cyc["chapters"][0]["prereqs"] = ["c3"]
    with pytest.raises(ValidationError, match="cycle"):
        OutlineOut.model_validate(cyc)
    selfp = outline(3)
    selfp["chapters"][1]["prereqs"] = ["c2"]
    with pytest.raises(ValidationError, match="itself"):
        OutlineOut.model_validate(selfp)
    unk = outline(3)
    unk["chapters"][1]["prereqs"] = ["zzz"]
    with pytest.raises(ValidationError, match="does not exist"):
        OutlineOut.model_validate(unk)
    dup = outline(3)
    dup["chapters"][2]["key"] = "c1"
    with pytest.raises(ValidationError, match="duplicate"):
        OutlineOut.model_validate(dup)


def test_is_acyclic():
    assert is_acyclic({"a": [], "b": ["a"], "c": ["a", "b"]})
    assert not is_acyclic({"a": ["b"], "b": ["a"]})


def test_slide_and_content_rules():
    with pytest.raises(ValidationError):
        Slide(title="x", points=["only one"])
    good = {"notes_md": "n" * 200, "summary": "s", "slides": [{"title": f"s{i}", "points": ["a", "b"]} for i in range(4)]}
    assert ChapterContent.model_validate(good)
    with pytest.raises(ValidationError, match="too short"):
        ChapterContent.model_validate({**good, "notes_md": "short"})
    with pytest.raises(ValidationError, match="slides"):
        ChapterContent.model_validate({**good, "slides": good["slides"][:2]})


def test_question_key_resolution_and_quiz_size():
    q = Question(id="q1", text="?", options=["x", "y", "z"], answer_key="B", concept="c", hint="h")
    assert resolve_key(q) == "y"
    with pytest.raises(ValidationError):
        Question(id="q1", text="?", options=["x", "y"], answer_key="nope", concept="c", hint="h")
    with pytest.raises(ValidationError):
        Question(id="q1", text="?", options=["x", "x"], answer_key="x", concept="c", hint="h")
    one = {"id": "q1", "text": "?", "options": ["a", "b"], "answer_key": "a", "concept": "c", "hint": "h"}
    with pytest.raises(ValidationError, match="3-12"):
        QuizOut.model_validate({"questions": [one]})


def test_critic_reject_needs_issue():
    with pytest.raises(ValidationError):
        CriticOut(verdict="reject", issues=[])


def test_rewriter_caps_inserts():
    ins = {"after": "1", "title": "Review: x"}
    with pytest.raises(ValidationError, match="at most"):
        RewriterOut.model_validate({"patch": {"insert": [ins, ins, ins]}})
