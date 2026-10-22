"""Critic: checks a chapter's content against its sources; approve, revise or reject.

A different model from the Teacher where possible (gemma_31b checks what gemma_26b wrote). Last resort: rule checks.
"""
from __future__ import annotations

import re

from ..router import ModelRouter, RouterResult
from ..schemas import MAX_NOTES_CHARS, ChapterContent, CriticOut, LearnerProfile
from . import gates
from .common import build_prompt, lang_name
from .teacher import VERBATIM_WORDS, content_text, longest_shared_run

# Phrases that signal the notes hand over a quiz answer.
LEAK_PATTERNS = [r"\bthe answer is\b", r"\banswer:\s", r"\bcorrect answer\b"]


def rule_checks(content: ChapterContent, chunks: list[dict], allowed_extra: set[str] = frozenset()) -> CriticOut:
    """Last resort (no model): refs exist, length cap, no answer leakage, no long verbatim copying of fetched pages."""
    known = {c["chunk_id"] for c in chunks} | set(allowed_extra)
    issues = []
    for r in content.source_refs:
        if r not in known:
            issues.append({"step": "refs", "problem": f"Source ref {r} does not exist", "source_ref": r})
    if known and not content.source_refs:
        issues.append({"step": "refs", "problem": "No source refs although sources exist"})
    if len(content.notes_md) > MAX_NOTES_CHARS:
        issues.append({"step": "notes", "problem": "Notes over the length cap"})
    text = content_text(content)
    if any(re.search(p, text, re.I) for p in LEAK_PATTERNS):
        issues.append({"step": "notes", "problem": "Possible quiz answer given too early"})
    for c in chunks:
        if c["chunk_id"].startswith("w") and longest_shared_run(text, c["text"]) >= VERBATIM_WORDS:
            issues.append({"step": "notes", "problem": f"Long verbatim copy of {c['chunk_id']}", "source_ref": c["chunk_id"]})
    return CriticOut(verdict="revise" if issues else "approve", issues=issues, confidence=0.4)


def run(router: ModelRouter, content: ChapterContent, chunks: list[dict], profile: LearnerProfile,
        allowed_extra: set[str] = frozenset()) -> RouterResult:
    ctx = {"content": content.model_dump(), "chunks": chunks}

    def validate(raw: dict) -> CriticOut:
        return CriticOut.model_validate(raw)

    # claim_support gate: only claims the sources do not support go to the Critic model. All supported -> code checks only.
    flagged = gates.claim_support(router, content.notes_md, chunks)
    total = len(gates.claim_sentences(content.notes_md)) if flagged is not None else None
    if flagged is not None and not flagged:
        router.note("critic", "ok", "claim_support: every claim is supported; Critic model not needed", "code checks (refs, length, leaks, verbatim) still ran")
        out = rule_checks(content, chunks, allowed_extra)
        out.claims_total, out.claims_flagged = total, 0
        return RouterResult(out, "claim_support", 0)
    if flagged:
        router.note("critic", "ok", f"claim_support flagged {len(flagged)} claim(s) for the Critic", "; ".join(c[:60] for c in flagged[:3]))

    example = {"verdict": "approve | revise | reject",
               "issues": [{"step": "notes", "problem": "Claim not found in source", "source_ref": "s1-p1-c1"}],
               "confidence": 0.85}
    prompt = build_prompt(
        "You are Critic, a strict fact-checker. Compare the chapter's notes and slides with the supplied source chunks. Flag any "
        "claim without support (start with `flagged_claims` when given: a support gate doubts them), any error, any quiz answer given too early, any long copied passage, and any wording too hard "
        "for the level. When there are no sources, check internal consistency and plain factual correctness. Be specific so the "
        "Teacher can fix it. Do not rewrite the content. 'reject' needs at least one issue.",
        example, {"flagged_claims": flagged, "chapter_content": ctx["content"], "source_chunks": [{"id": c["chunk_id"], "text": c["text"]} for c in chunks],
                  "level": profile.level, "student_language": lang_name(profile.language)})
    res = router.call("critic", prompt, ctx, validate,
                      last_resort=lambda c: rule_checks(content, chunks, allowed_extra), summary="Critic review of chapter")
    if flagged is not None:
        res.value.claims_total, res.value.claims_flagged = total, len(flagged)
    return res
