"""06 Diagnoser: grade the answer and name the misconception (agents/06-diagnoser.md).

MCQ answers are always graded in code first. The model is only asked WHY an
answer is wrong (misconception label + hint), so grading never depends on it.
"""
from __future__ import annotations

from typing import Optional

from ..providers.typesafe import choice, noul, score
from ..router import ModelRouter, RouterResult
from ..quiz_grading import correct_text, grade
from ..schemas import DiagnoserOut, Question
from .common import build_prompt

LOW_CONFIDENCE = 0.5  # below this the orchestrator asks a follow-up instead of a verdict


def is_correct(q: Question, student_answer: str) -> bool:
    return grade(q, student_answer) >= 1.0


def _jev_diagnose(router: ModelRouter, q: Question, student_answer: str, known_labels: list[str]) -> Optional[DiagnoserOut]:
    """One Jev call with three typed questions (correct? / which known misconception? / how complete?).
    Returns None when Jev is unavailable, unsure, or the mistake is not in the known labels: the Gemma chain
    then proposes a new label. Jev returns typed answers only, so the hint comes from the Quizzer's question."""
    if not known_labels:
        return None
    options = {lab: lab for lab in known_labels[:20]}
    options["other"] = "A mistake that is not in the list"
    state = {"question": q.text, "options": q.options, "correct_answer": correct_text(q), "concept": q.concept,
             "student_answer": student_answer}
    answers = router.decide("diagnoser", state, {
        "correct": noul("Is the student's answer correct for this question?"),
        "misconception": choice("Which misconception best explains the student's wrong answer?", options),
        "completeness": score("How complete is the student's understanding shown by the answer?",
                              ["Not at all", "Partly", "Fully"]),
    }, summary=f"diagnose {q.id}", student_data=True)  # the student's answer: never sent to the free-tier decision model
    if not answers:
        return None
    try:
        label = answers["misconception"]["choice"]
        conf = float(answers["misconception"]["confidence"])
        partial = float(answers["completeness"]["score"]) >= 1.5
    except (KeyError, TypeError, ValueError):
        return None
    if label == "other" or label not in known_labels:
        return None
    return DiagnoserOut(question_id=q.id, result="partial" if partial else "wrong", misconception=label,
                        confidence=max(0.0, min(1.0, conf)), hint=q.hint)


def run(router: ModelRouter, q: Question, student_answer: str, known_labels: list[str]) -> RouterResult:
    g = grade(q, student_answer)
    if g >= 1.0:
        # Code-graded; no model call needed.
        out = DiagnoserOut(question_id=q.id, result="correct", misconception=None, confidence=1.0, hint="")
        return RouterResult(out, "code", 0)
    if 0 < g < 1:                      # part of a multi-select or an ordering is right: partial credit, graded in code too
        return RouterResult(DiagnoserOut(question_id=q.id, result="partial", misconception=None, confidence=1.0, hint=q.hint), "code", 0)
    jev = _jev_diagnose(router, q, student_answer, known_labels)
    if jev:
        return RouterResult(jev, "jev", 0)

    ctx = {"question": q.model_dump(), "student_answer": student_answer, "known_labels": known_labels}

    def validate(raw: dict) -> DiagnoserOut:
        out = DiagnoserOut.model_validate({**raw, "question_id": q.id})
        if out.result == "correct":
            raise ValueError("answer was already graded wrong in code")  # code wins over the model
        if out.result == "wrong" and not (out.misconception or "").strip():
            raise ValueError("a wrong result needs a misconception label")
        return out

    def exact_match(c: dict) -> DiagnoserOut:
        """Last resort: graded in code, no misconception label."""
        return DiagnoserOut(question_id=q.id, result="wrong", misconception=None, confidence=1.0, hint=q.hint)

    example = {"question_id": q.id, "result": "wrong", "misconception": "confuses speed and velocity",
               "confidence": 0.8, "hint": "Short nudge that does not reveal the answer"}
    prompt = build_prompt(
        "You are Diagnoser. The student's answer was graded wrong in code. Decide if it is 'wrong' or 'partial'. "
        "Choose the closest misconception from known_labels, or propose a new SHORT label. Rate confidence 0 to 1. "
        "Write a one-line hint that does not reveal the answer.",
        example, {"question": ctx["question"], "student_answer": student_answer, "known_labels": known_labels})
    return router.call("diagnoser", prompt, ctx, validate, last_resort=exact_match,
                       summary=f"Diagnose {q.id}")
