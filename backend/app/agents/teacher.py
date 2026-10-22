"""Teacher: writes one chapter's notes, slide cards and summary from the student's material and verified resources.

Own words only. Third-party page text is used to check facts, never pasted: a verbatim run of 40+ words from a
fetched page is rejected in code. Indian-language students use the `teacher_indic` chain (Sarvam-105B first).
"""
from __future__ import annotations

import re
from typing import Optional

from ..db import Store
from ..router import ModelRouter, RouterResult
import os

from ..schemas import ChapterContent, LearnerProfile
from .common import build_prompt, fingerprint, lang_name, split_sentences

VERBATIM_WORDS = 40


def content_cache_key(chapter: dict, language: str) -> str:
    return f"chapter:{fingerprint([{'text': chapter['title'] + '|' + chapter.get('summary', '')}])}:{language}"


def longest_shared_run(text: str, source: str) -> int:
    """Length (in words) of the longest verbatim word run shared by `text` and `source`."""
    a = re.findall(r"\w+", text.lower())
    b = re.findall(r"\w+", source.lower())
    if not a or not b:
        return 0
    index: dict[str, list[int]] = {}
    for i, w in enumerate(b):
        index.setdefault(w, []).append(i)
    best = 0
    for i, w in enumerate(a):
        for j in index.get(w, []):
            k = 0
            while i + k < len(a) and j + k < len(b) and a[i + k] == b[j + k]:
                k += 1
            best = max(best, k)
            if best >= 200:
                return best
    return best


def content_text(c: ChapterContent) -> str:
    return " ".join([c.notes_md, c.summary] + [s.title + " " + " ".join(s.points) for s in c.slides])


def run(
    router: ModelRouter,
    chapter: dict,
    chunks: list[dict],
    link_refs: list[dict],
    profile: LearnerProfile,
    issues: Optional[list[dict]] = None,
    previous: Optional[dict] = None,
    store: Optional[Store] = None,
    depth: Optional[dict] = None,
) -> RouterResult:
    """chunks: material the notes may rest on (student's chunks + fetched page chunks).
    link_refs: verified resources the notes may point to: [{"ref": "url:...", "title", "source"}]."""
    allowed = {c["chunk_id"] for c in chunks} | {l["ref"] for l in link_refs}
    third_party = [c for c in chunks if c["chunk_id"].startswith("w")]
    ctx = {"chapter": {k: chapter.get(k) for k in ("id", "title", "summary", "objectives")}, "chunks": chunks,
           "allowed_refs": sorted(allowed), "language": profile.language, "issues": issues or [], "level": profile.level,
           "goal": profile.goal}

    def validate(raw: dict) -> ChapterContent:
        lang = raw.pop("language", None) if isinstance(raw, dict) else None
        if lang and lang != profile.language:
            raise ValueError(f"language must be {profile.language}, got {lang}")
        out = ChapterContent.model_validate(raw)
        bad = [r for r in out.source_refs if r not in allowed]
        if bad:
            raise ValueError(f"unknown source refs {bad[:3]}")
        if allowed and not out.source_refs:
            raise ValueError("cite at least one source ref")
        text = content_text(out)
        for c in third_party:
            n = longest_shared_run(text, c["text"])
            if n >= VERBATIM_WORDS:
                raise ValueError(f"{n} words copied verbatim from {c['chunk_id']}; write in your own words")
        return out

    def cached_lesson(c: dict) -> ChapterContent:
        """Last resort: a previously approved version of this chapter, else a plain extract of the sources."""
        if store:
            hit = store.cache_get(content_cache_key(chapter, profile.language))
            if hit:
                return ChapterContent.model_validate(hit)
        own = [c for c in chunks if not c["chunk_id"].startswith("w")]  # never copy fetched third-party pages
        sents = [s for ch in own for s in split_sentences(ch["text"])]
        objectives = chapter.get("objectives") or []
        if sents:
            notes = f"## {chapter['title']}\n\n" + " ".join(sents[:6])
            points = sents[:8]
        else:  # nothing to ground on and no model: say so plainly instead of inventing facts
            notes = (f"## {chapter['title']}\n\n{chapter.get('summary', '')}\n\nIn this chapter you will: "
                     + "; ".join(objectives or [chapter["title"]]) + ". Detailed notes could not be generated right now; use \"Try again\".")
            points = (objectives or [chapter["title"]]) + [chapter.get("summary") or chapter["title"]]
        notes = notes.ljust(130, " .") if len(notes) < 130 else notes
        slides = []
        for i in range(0, min(len(points), 8), 2):
            pts = points[i:i + 2] or [chapter["title"], chapter.get("summary") or chapter["title"]]
            if len(pts) < 2:
                pts.append(chapter["title"])
            slides.append({"title": chapter["title"] if i == 0 else f"{chapter['title']} ({i // 2 + 1})", "points": [p[:160] for p in pts]})
        while len(slides) < 3:
            slides.append({"title": chapter["title"], "points": [chapter["title"], chapter.get("summary") or "See the notes"]})
        refs = [c["chunk_id"] for c in own[:2]]
        return ChapterContent.model_validate({"notes_md": notes, "slides": slides[:8], "key_points": points[:4],
                                              "summary": (sents[0] if sents else chapter.get("summary") or chapter["title"])[:300],
                                              "source_refs": refs})

    example = {"language": profile.language, "notes_md": "## Heading\n\nShort paragraphs in your own words. Use lists and **bold** for key terms.",
               "slides": [{"title": "Slide title", "points": ["Short point", "Short point"]}],
               "key_points": ["..."], "summary": "Two or three sentences.", "source_refs": ["<an allowed ref>"]}
    depth = depth or {"words": 600, "examples": 1, "slides": 6}
    typed = os.environ.get("AASAAN_TYPED_NOTES", "on").strip().lower() not in ("off", "0", "false", "no")
    if typed:
        notes_ask = (f"Produce `sections` (about {depth['words']} words in total, own words, one idea per paragraph, short sentences, plain language): a list of "
                     "{kind, body} with these kinds in this order: why_it_matters (why a learner needs this), key_terms (one `term: meaning` per line), explanation, "
                     f"worked_example ({depth['examples']} example{'s' if depth['examples'] != 1 else ''}, step by step), common_mistakes (one per line: what learners get wrong and why), "
                     "recap (3-4 sentences), and optionally go_deeper. Leave `notes_md` empty. Also return `common_mistakes` as a short list of labels. ")
        example = {**example, "notes_md": "", "sections": [{"kind": "why_it_matters", "body": "..."}, {"kind": "explanation", "body": "..."}], "common_mistakes": ["..."]}
    else:
        notes_ask = f"Produce `notes_md` (markdown, about {depth['words']} words, own words, clear headings, short paragraphs, {depth['examples']} worked example(s)); "
    job = (f"You are Teacher. Write the notes for ONE chapter of a course, for a {profile.level} student. "
           + notes_ask +
           f"`slides` ({max(4, min(depth['slides'], 16))} cards, each a title and 2-4 short points); `key_points`; `summary` (2-3 sentences). "
           "Use ONLY facts supported by the supplied source chunks and verified resources; never invent facts and never paste "
           "long passages (quote at most a short phrase with attribution). Put the refs you relied on in `source_refs` "
           "(only from allowed_refs). Do not give away quiz answers. "
           f"Write all student-facing text in {lang_name(profile.language)}; keep JSON field names in English.")
    if issues:
        job += " This is a REVISION: fix only the issues the Critic listed and keep the rest."
    prompt = build_prompt(job, example, {
        "chapter": ctx["chapter"], "goal": profile.goal, "source_chunks": [{"id": c["chunk_id"], "text": c["text"]} for c in chunks],
        "verified_resources": link_refs, "allowed_refs": ctx["allowed_refs"], "critic_issues": issues or [], "previous_draft": previous})
    return router.call("teacher", prompt, ctx, validate, last_resort=cached_lesson,
                       summary=f"Notes for '{chapter['title'][:40]}'" + (" (revision)" if issues else ""),
                       language=profile.language)
