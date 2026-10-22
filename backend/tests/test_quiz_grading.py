"""Quiz marking in code: types, partial credit, weights, per-concept results, mastery."""
import pytest
from pydantic import ValidationError

from app import quiz_grading as qg
from app.schemas import Question


def q(**kw):
    base = dict(id="q1", text="?", concept="c", hint="h", options=["a", "b", "c", "d"], answer_key="b")
    return Question(**{**base, **kw})


def test_every_type_is_graded_in_code_with_partial_credit():
    assert qg.grade(q(), "b") == 1.0 and qg.grade(q(), "B") == 1.0 and qg.grade(q(), "a") == 0.0                  # letter answers still work
    m = q(type="multi", answer_key="a || c")
    assert qg.grade(m, "a || c") == 1.0 and qg.grade(m, "a") == 0.5 and qg.grade(m, "a || b") == 0.0 and qg.grade(m, "a || c || b") == 0.5
    o = q(type="order", options=["x", "y", "z", "w"], answer_key="y || x || z")
    assert qg.grade(o, "y || x || z") == 1.0 and round(qg.grade(o, "x || y || z"), 2) == 0.33 and qg.grade(o, "y || x") == 0.0
    f = q(type="fill_blank", options=[], answer_key="O(log n) || logarithmic")
    assert qg.grade(f, " o(log n) ") == 1.0 and qg.grade(f, "Logarithmic!") == 1.0 and qg.grade(f, "linear") == 0.0
    assert qg.correct_text(m) == "a, c" and qg.correct_text(o) == "y → x → z"


def test_bad_questions_are_rejected_at_the_door():
    with pytest.raises(ValidationError):
        q(type="multi", answer_key="a || zzz")                                                                   # a key that is not an option
    with pytest.raises(ValidationError):
        q(type="order", options=["x", "y"], answer_key="x || y")
    with pytest.raises(ValidationError):
        q(difficulty=7)


def test_marks_weight_by_difficulty_and_name_the_weakest_objective():
    qs = [{"id": "q1", "concept": "arrays", "objective": "Index an array", "difficulty": 1}, {"id": "q2", "concept": "arrays", "objective": "Index an array", "difficulty": 3},
          {"id": "q3", "concept": "lists", "difficulty": 2}]
    att = [{"question_id": "q1", "result": "correct"}, {"question_id": "q2", "result": "wrong"}, {"question_id": "q2", "result": "correct"}, {"question_id": "q3", "result": "partial"}]
    m = qg.mark(att, qs)
    assert (m["earned"], m["possible"], m["percent"], m["passed"]) == (1.75, 4.5, 39, False)                    # only the FIRST try of each question counts
    assert {r["concept"]: r["percent"] for r in m["by_concept"]} == {"Index an array": 33, "lists": 50} and m["weakest"] == ["Index an array", "lists"]
    assert m["missed"] == ["q2", "q3"]
    assert qg.mark([{"question_id": "q1", "result": "correct"}], qs[:1])["passed"] is True


def test_finishing_a_quiz_returns_marks_and_updates_mastery(engine, worker, store):
    nid = engine.create_notebook("L1", "Teach me binary search", "en", [])
    worker.run_until_idle()
    cid = store.chapters(nid)[0]["id"]
    sid = engine.store.open_session("L1")
    qs = next(b for b in store.blocks(cid) if b["type"] == "quiz")["content"]["questions"]
    for item in qs:
        engine.answer("L1", sid, cid, item["id"], Question.model_validate(item).answer_key)
    out = engine.finish_chapter("L1", sid, cid)
    assert out["marks"]["percent"] == 100 and out["marks"]["passed"] and out["marks"]["weakest"] == []
    assert store.get_memory("L1", "mastery", qs[0]["concept"])["pct"] == 100
