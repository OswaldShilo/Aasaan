"""Quiz marking in code: question types, partial credit, difficulty weights, per-concept results. No model decides a mark.

Answer encoding (what the page sends): mcq and true/false: the option text (or a letter A-D); multi: the chosen options joined by " || ";
order: the items in the learner's order joined by " || "; fill_blank: the typed text. For multi, order and fill_blank the question's `answer_key`
is the correct options / the correct order / the accepted answers, also joined by " || ".
"""
from __future__ import annotations

import re
from typing import Optional

SEP = " || "
PASS_PERCENT = 70
WEIGHT = {1: 1.0, 2: 1.5, 3: 2.0}          # points by difficulty


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s%.+\-*/=<>^()]", "", (s or "").lower())).strip()


def key_items(answer_key: str) -> list[str]:
    return [p.strip() for p in (answer_key or "").split("||") if p.strip()]


def _mcq_key(q) -> Optional[str]:
    if q.answer_key in q.options:
        return q.answer_key
    k = q.answer_key.strip().upper()
    if len(k) == 1 and "A" <= k <= "Z" and ord(k) - ord("A") < len(q.options):
        return q.options[ord(k) - ord("A")]
    return None


def grade(q, answer: str) -> float:
    """0.0 to 1.0. Partial credit for multi-select (right picks minus wrong picks) and ordering (share of items in the right place)."""
    a = (answer or "").strip()
    t = getattr(q, "type", "mcq")
    if t == "mcq":
        key = _mcq_key(q)
        if key is not None and a.lower() == key.lower():
            return 1.0
        if len(a) == 1 and a.isalpha():
            idx = ord(a.upper()) - ord("A")
            return 1.0 if idx < len(q.options) and q.options[idx] == key else 0.0
        return 0.0
    if t == "multi":
        right, picked = {_norm(x) for x in key_items(q.answer_key)}, {_norm(x) for x in a.split("||") if x.strip()}
        if not right:
            return 0.0
        return max(0.0, (len(right & picked) - len(picked - right)) / len(right))
    if t == "order":
        want, got = [_norm(x) for x in key_items(q.answer_key)], [_norm(x) for x in a.split("||") if x.strip()]
        if not want or len(got) != len(want):
            return 0.0
        return sum(1 for w, g in zip(want, got) if w == g) / len(want)
    if t == "fill_blank":
        return 1.0 if _norm(a) in {_norm(x) for x in key_items(q.answer_key)} and _norm(a) else 0.0
    return 0.0


def correct_text(q) -> str:
    """The answer to show once the learner has tried twice."""
    t = getattr(q, "type", "mcq")
    if t == "mcq":
        return _mcq_key(q) or ""
    if t == "multi":
        return ", ".join(key_items(q.answer_key))
    if t == "order":
        return " → ".join(key_items(q.answer_key))
    return " / ".join(key_items(q.answer_key))


def mark(attempts: list[dict], questions: list[dict]) -> dict:
    """Score a finished quiz. `attempts` are the stored attempts (the first try of each question counts), `questions` the quiz block's questions.
    Returns {earned, possible, percent, passed, by_concept: [{concept, earned, possible, percent}], weakest: [concept...], missed: [question ids]}."""
    first: dict[str, dict] = {}
    for r in attempts:
        first.setdefault(r["question_id"], r)
    earned = possible = 0.0
    by: dict[str, list[float]] = {}
    missed = []
    for q in questions:
        w = WEIGHT.get(int(q.get("difficulty") or 1), 1.0)
        r = first.get(q["id"])
        got = 0.0 if r is None else (1.0 if r["result"] == "correct" else 0.5 if r["result"] == "partial" else 0.0)
        possible += w
        earned += w * got
        e = by.setdefault(q.get("objective") or q.get("concept") or "Other", [0.0, 0.0])
        e[0] += w * got
        e[1] += w
        if got < 1.0:
            missed.append(q["id"])
    rows = [{"concept": k, "earned": round(v[0], 2), "possible": round(v[1], 2), "percent": round(100 * v[0] / v[1]) if v[1] else 0} for k, v in by.items()]
    pct = round(100 * earned / possible) if possible else 0
    return {"earned": round(earned, 2), "possible": round(possible, 2), "percent": pct, "passed": pct >= PASS_PERCENT,
            "by_concept": rows, "weakest": [r["concept"] for r in sorted(rows, key=lambda r: r["percent"]) if r["percent"] < 100][:3], "missed": missed}
