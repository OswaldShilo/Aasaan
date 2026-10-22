"""Quizzer: 3 to 5 multiple-choice questions per chapter with answer keys, concept tags and hints."""
from __future__ import annotations

import os
from typing import Optional

from ..db import Store
from ..router import ModelRouter, RouterResult
from ..schemas import ChapterContent, LearnerProfile, QuizOut
from .common import build_prompt, fingerprint, lang_name


def quiz_cache_key(chapter: dict, language: str) -> str:
    return f"quiz:{fingerprint([{'text': chapter['title'] + '|' + chapter.get('summary', '')}])}:{language}"


def run(router: ModelRouter, chapter: dict, content: ChapterContent, chunks: list[dict], profile: LearnerProfile,
        store: Optional[Store] = None, depth: Optional[dict] = None) -> RouterResult:
    known = {c["chunk_id"] for c in chunks}
    ctx = {"chapter": {k: chapter.get(k) for k in ("id", "title", "summary", "objectives")}, "content": content.model_dump(),
           "chunks": chunks, "mistake_labels": profile.mistake_labels}

    def validate(raw: dict) -> QuizOut:
        out = QuizOut.model_validate(raw)
        for q in out.questions:
            if q.source_ref and q.source_ref not in known and not q.source_ref.startswith("url:"):
                q.source_ref = None  # tolerate a stray ref rather than reject a good question
        return out

    def cached_questions(c: dict) -> QuizOut:
        """Last resort: cached quiz for this chapter, else questions built in code from the key points."""
        if store:
            hit = store.cache_get(quiz_cache_key(chapter, profile.language))
            if hit:
                return QuizOut.model_validate(hit)
        points = [p for p in (content.key_points or [s.points[0] for s in content.slides]) if p] or [chapter["title"]]
        decoys = ["The opposite of what the notes say", "Something the notes never mention", "None of these"]
        qs = []
        for i in range(3):
            pt = points[i % len(points)]
            correct = pt if len(pt) < 140 else pt[:137] + "..."
            qs.append({"id": f"q{i + 1}", "type": "mcq", "text": f"Which statement matches the notes on '{chapter['title']}'?",
                       "options": [correct] + [d if i == 0 else f"{d} ({i})" for d in decoys], "answer_key": correct,
                       "concept": chapter["title"], "hint": "Re-read the notes and look for the key words.",
                       "source_ref": content.source_refs[0] if content.source_refs else None})
        return QuizOut.model_validate({"questions": qs})

    example = {"questions": [{"id": "q1", "type": "mcq", "text": "...", "options": ["opt 1", "opt 2", "opt 3", "opt 4"],
                              "answer_key": "opt 2", "concept": "short concept tag", "hint": "A nudge that does not give the answer",
                              "source_ref": "<chunk id or null>"}]}
    n = max(3, min((depth or {}).get("quiz", 5), 12))
    mixed = os.environ.get("AASAAN_QUIZ_TYPES", "on").strip().lower() not in ("off", "0", "false", "no")
    kinds = ("Use a mix of types: `mcq` (exactly one correct option, answer_key = the exact option text), `multi` (select all that apply: answer_key = the correct options joined by ' || ', "
             "options include 1-2 wrong ones), `order` (put 3-5 steps in order: options in a shuffled order, answer_key = the steps in the right order joined by ' || ') and `fill_blank` "
             "(short answer: options = [], answer_key = accepted answers joined by ' || '). Use mostly mcq. " if mixed else "Every question is `mcq`. ")
    prompt = build_prompt(
        f"You are Quizzer. Write {n} questions that test understanding of this chapter, not recall of wording. " + kinds +
        "Mix difficulty (`difficulty` 1 easy, 2 medium, 3 hard) and set `objective` to the chapter objective each one tests. If mistake_labels exist, aim one question at one of them. "
        "Each question has a concept tag, a gentle hint that does not give the answer away, and a one-sentence `explanation`. Wrong options should be the common mistakes. "
        f"Write question text in {lang_name(profile.language)}.",
        example, {"chapter": ctx["chapter"], "notes": content.notes_md[:3000], "key_points": content.key_points,
                  "source_chunks": [{"id": c["chunk_id"], "text": c["text"][:600]} for c in chunks[:6]],
                  "mistake_labels": profile.mistake_labels[:3]})
    return router.call("quizzer", prompt, ctx, validate, last_resort=cached_questions,
                       summary=f"Quiz for '{chapter['title'][:40]}'", language=profile.language)
