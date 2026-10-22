"""Slides: a deck per chapter, written AFTER the notes are final, so it never delays the first read.

Two steps for quality: a plan (titles and slide types, which guarantees the count and the variety), then the cards. Code enforces the rules:
at most 4 bullets of 12 words, a clear first and last slide, and a similarity check that drops slides that mostly copy the notes. A deck under the
depth profile's minimum gets one expansion pass; whatever exists is shipped (never an empty block). Falls back to the Teacher's own slides.
"""
from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..router import ModelRouter, RouterResult
from ..schemas import ChapterContent, LearnerProfile
from .common import build_prompt, lang_name

TYPES = ("title", "concept", "example", "diagram", "comparison", "code", "mistake", "recap", "check")
ADDED_VALUE = {"example", "diagram", "comparison", "code", "mistake", "check"}      # slides that bring something the notes paragraph does not
MAX_BULLETS, MAX_WORDS = 4, 12
SIMILAR_ABOVE = 0.6                                                                # share of a slide's 4-word runs found verbatim in the notes


class Card(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: Literal["title", "concept", "example", "diagram", "comparison", "code", "mistake", "recap", "check"] = "concept"
    title: str
    points: list[str] = Field(min_length=1)
    note: str = ""                      # optional speaker notes


class Deck(BaseModel):
    model_config = ConfigDict(extra="ignore")
    slides: list[Card]


def _grams(text: str, n: int = 4) -> set[tuple[str, ...]]:
    w = re.findall(r"[^\W_]+", text.lower())
    return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}


def similarity(slide_text: str, notes: str) -> float:
    """How much of the slide is a verbatim copy of the notes (0 to 1). Slides under 4 words cannot be judged and count as 0."""
    g = _grams(slide_text)
    return len(g & _grams(notes)) / len(g) if g else 0.0


def tidy(cards: list[Card], notes_md: str) -> tuple[list[dict], list[str]]:
    """Enforce the card rules in code. Returns (cards as dicts, plain-language reasons for anything dropped or trimmed)."""
    out, why = [], []
    for c in cards:
        pts = [" ".join(p.split()[:MAX_WORDS]) for p in c.points if p.strip()][:MAX_BULLETS]
        if not pts:
            continue
        if c.type not in ("title", "recap") and similarity(" ".join(pts), notes_md) > SIMILAR_ABOVE:
            why.append(f"dropped '{c.title[:30]}': it repeats the notes")
            continue
        out.append({"type": c.type, "title": c.title.strip()[:70], "points": pts, "note": c.note.strip()[:400]})
    return out, why


def fallback_cards(content: ChapterContent) -> list[dict]:
    """The Teacher's own slides (always valid), typed in code."""
    cards = []
    for i, s in enumerate(content.slides):
        cards.append({"type": "title" if i == 0 else "recap" if i == len(content.slides) - 1 else "concept", "title": s.title, "points": s.points[:MAX_BULLETS], "note": ""})
    return cards


def run(router: ModelRouter, chapter: dict, content: ChapterContent, profile: LearnerProfile, depth: dict) -> RouterResult:
    want = max(6, min(depth.get("slides", 10), 16))
    notes = content.notes_md
    ctx = {"chapter": chapter.get("title"), "want": want}

    def validate(raw: dict) -> Deck:
        deck = Deck.model_validate(raw)
        cards, _ = tidy(deck.slides, notes)
        if len(cards) < max(3, want - 3):
            raise ValueError(f"need about {want} slides that do not repeat the notes; {len(cards)} are usable")
        if not any(c["type"] in ADDED_VALUE for c in cards):
            raise ValueError("add at least one example, comparison, diagram, code or mistake slide")
        return Deck(slides=[Card.model_validate(c) for c in cards])

    def fallback(_c: dict) -> Deck:
        return Deck(slides=[Card.model_validate(c) for c in fallback_cards(content)])

    example = {"slides": [{"type": "title", "title": "Chapter title", "points": ["What you will learn"], "note": "optional speaker note"},
                          {"type": "example", "title": "A worked example", "points": ["Step one", "Step two"]}]}
    job = (f"You are Slides. Make a slide deck of {want} slides for this chapter, for a {profile.level} student. First plan the slides (title and type), then write each. "
           f"Types: {', '.join(TYPES)}. Start with a `title` slide (objectives) and end with a `recap`; include an example, a comparison or diagram idea, a common mistake, and a `check` slide "
           f"(3 quick self-check questions). Each slide has AT MOST {MAX_BULLETS} bullets of at most {MAX_WORDS} words, one takeaway, and must add something (an example, a comparison, a code "
           "snippet or a mistake): do NOT copy sentences from the notes. Use only facts in the notes. "
           f"Write all student-facing text in {lang_name(profile.language)}; keep JSON field names in English.")
    prompt = build_prompt(job, example, {"chapter": chapter.get("title"), "objectives": chapter.get("objectives"), "notes": notes[:6000], "key_points": content.key_points,
                                         "common_mistakes": content.common_mistakes})
    res = router.call("slides", prompt, ctx, validate, last_resort=fallback, summary=f"Slides for '{str(chapter.get('title'))[:40]}'", language=profile.language)
    cards, _ = tidy(res.value.slides, notes)
    return RouterResult(Deck(slides=[Card.model_validate(c) for c in cards]), res.model_used, res.fallback_step, res.used_last_resort) if cards else res
