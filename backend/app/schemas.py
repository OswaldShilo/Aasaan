"""Per-agent output schemas. Every agent reply is validated here; an invalid reply makes the router move on.

References to source material are plain strings: a chunk id like `s1a2b3-p4-c2` (the student's own material or a
fetched page) or `url:https://...` (a verified link). Internal JSON stays in English.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MIN_CHAPTERS, MAX_CHAPTERS = 3, 20
MAX_NOTES_CHARS = 14000          # the deep profile asks for about 1,800 words
MAX_MESSAGE_CHARS = 400
MAX_INSERTS_PER_SESSION = 2
MAX_CHAPTERS_AFTER_PATCH = 24


class Strict(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ------------------------------------------------------------------ source text
class Chunk(Strict):
    chunk_id: str
    page: int = 1
    text: str
    kind: Literal["text", "figure"] = "text"


class TopicOut(Strict):
    id: str
    title: str
    summary: str = ""
    page_refs: list[int] = Field(default_factory=list)


class IntakeOut(Strict):
    """What Intake extracts from ONE source: topics plus (for photos) the transcribed chunks."""
    source_id: str
    language: str = "en"
    topics: list[TopicOut]
    chunks: list[Chunk]
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self):
        if not self.topics:
            raise ValueError("at least one topic required")
        if len(self.topics) > 8:
            raise ValueError("at most 8 topics")
        if any(not c.text.strip() for c in self.chunks):
            raise ValueError("empty chunk")
        pages = {c.page for c in self.chunks}
        for t in self.topics:
            if not t.page_refs or any(p not in pages for p in t.page_refs):
                raise ValueError(f"topic {t.id} has a page ref not present in chunks")
        return self


# ------------------------------------------------------------------- Architect
class OutlineChapter(Strict):
    key: str
    title: str
    summary: str = ""
    objectives: list[str] = Field(default_factory=list)
    prereqs: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def _title(cls, v):
        if not v.strip():
            raise ValueError("chapter title is empty")
        return v.strip()


class OutlineOut(Strict):
    title: str
    description: str = ""
    level: Literal["beginner", "intermediate", "advanced"] = "beginner"
    chapters: list[OutlineChapter]

    @model_validator(mode="after")
    def _check(self):
        n = len(self.chapters)
        if not MIN_CHAPTERS <= n <= MAX_CHAPTERS:
            raise ValueError(f"outline needs {MIN_CHAPTERS}-{MAX_CHAPTERS} chapters, got {n}")
        keys = [c.key for c in self.chapters]
        if len(set(keys)) != n:
            raise ValueError("duplicate chapter keys")
        for c in self.chapters:
            for p in c.prereqs:
                if p not in keys:
                    raise ValueError(f"prereq {p} of {c.key} does not exist")
                if p == c.key:
                    raise ValueError(f"{c.key} lists itself as a prerequisite")
        if not is_acyclic({c.key: c.prereqs for c in self.chapters}):
            raise ValueError("prerequisite graph has a cycle")
        if not self.title.strip():
            raise ValueError("title is empty")
        return self


def is_acyclic(graph: dict) -> bool:
    """Kahn's algorithm. graph: node -> list of prerequisite nodes."""
    indeg = {n: len(set(p)) for n, p in graph.items()}
    succ: dict = {n: [] for n in graph}
    for n, prereqs in graph.items():
        for p in set(prereqs):
            succ[p].append(n)
    queue = [n for n, d in indeg.items() if d == 0]
    seen = 0
    while queue:
        cur = queue.pop()
        seen += 1
        for s in succ[cur]:
            indeg[s] -= 1
            if indeg[s] == 0:
                queue.append(s)
    return seen == len(graph)


# --------------------------------------------------------------------- Curator
class CuratorOut(Strict):
    """Gemma fallback for relevance gating when Jev is unavailable: ids of candidates worth keeping."""
    relevant: list[str]


# --------------------------------------------------------------------- Teacher
class Slide(Strict):
    title: str
    points: list[str]
    image_query: Optional[str] = None

    @model_validator(mode="after")
    def _check(self):
        if not 2 <= len(self.points) <= 4:
            raise ValueError("a slide needs 2-4 points")
        if not self.title.strip() or any(not p.strip() for p in self.points):
            raise ValueError("empty slide text")
        return self


class NoteSection(Strict):
    kind: Literal["why_it_matters", "key_terms", "explanation", "worked_example", "common_mistakes", "recap", "go_deeper"]
    body: str


class ChapterContent(Strict):
    notes_md: str = ""
    slides: list[Slide]
    key_points: list[str] = Field(default_factory=list)
    summary: str
    source_refs: list[str] = Field(default_factory=list)
    sections: list[NoteSection] = Field(default_factory=list)       # typed notes: code renders them into notes_md (readability.render_sections)
    common_mistakes: list[str] = Field(default_factory=list)        # become the Diagnoser's misconception labels

    @model_validator(mode="after")
    def _check(self):
        if self.sections and not self.notes_md.strip():
            from .readability import render_sections
            kinds = {s.kind for s in self.sections}
            if "explanation" not in kinds:
                raise ValueError("typed notes need an explanation section")
            self.notes_md = render_sections([s.model_dump() for s in self.sections])
        if len(self.notes_md.strip()) < 120:
            raise ValueError("notes are too short")
        if len(self.notes_md) > MAX_NOTES_CHARS:
            raise ValueError("notes are over the length cap")
        if not 3 <= len(self.slides) <= 16:
            raise ValueError(f"need 3-16 slides, got {len(self.slides)}")
        if not self.summary.strip():
            raise ValueError("summary is empty")
        return self


# ---------------------------------------------------------------------- Critic
class CriticIssue(Strict):
    step: int | str = 0
    problem: str
    source_ref: Optional[str] = None


class CriticOut(Strict):
    verdict: Literal["approve", "revise", "reject"]
    issues: list[CriticIssue] = Field(default_factory=list)
    confidence: float = 0.5
    claims_total: Optional[int] = None      # set by code (not the model): how many claim sentences the notes made...
    claims_flagged: Optional[int] = None    # ...and how many the sources did not support (None when the gate could not run)

    @model_validator(mode="after")
    def _check(self):
        if self.verdict == "reject" and not self.issues:
            raise ValueError("reject requires at least one issue")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence out of range")
        return self


# -------------------------------------------------------------------- Quizzer
class Question(Strict):
    id: str
    type: Literal["mcq", "multi", "order", "fill_blank"] = "mcq"
    text: str
    options: list[str] = Field(default_factory=list)
    answer_key: str
    concept: str
    hint: str
    source_ref: Optional[str] = None
    difficulty: int = 1                    # 1 easy, 2 medium, 3 hard: points are weighted by it
    objective: Optional[str] = None        # which chapter objective this tests (the results screen groups by it)
    explanation: str = ""                  # shown after the answer, with the misconception named by the Diagnoser

    @model_validator(mode="after")
    def _check(self):
        if self.difficulty not in (1, 2, 3):
            raise ValueError(f"{self.id}: difficulty is 1, 2 or 3")
        if self.type == "fill_blank":
            if not self.answer_key.strip():
                raise ValueError(f"{self.id}: fill_blank needs at least one accepted answer")
        elif self.type in ("multi", "order"):
            keys = [k.strip() for k in self.answer_key.split("||") if k.strip()]
            need = 2 if self.type == "multi" else 3
            if len(self.options) < need + (1 if self.type == "multi" else 0) or len(set(self.options)) != len(self.options) or len(keys) < need or any(k not in self.options for k in keys):
                raise ValueError(f"{self.id}: {self.type} needs unique options and an answer_key made of those options joined by ' || '")
        else:
            if len(self.options) < 2 or len(set(self.options)) != len(self.options):
                raise ValueError(f"{self.id}: options must be unique, at least 2")
            if resolve_key(self) is None:
                raise ValueError(f"{self.id}: answer_key does not match exactly one option")
        if not self.concept.strip() or not self.hint.strip():
            raise ValueError(f"{self.id}: concept and hint are required")
        return self


def resolve_key(q: Question) -> Optional[str]:
    """Return the correct option text. answer_key may be the option text or a letter A-D."""
    if q.answer_key in q.options:
        return q.answer_key
    k = q.answer_key.strip().upper()
    if len(k) == 1 and "A" <= k <= "Z":
        idx = ord(k) - ord("A")
        if idx < len(q.options):
            return q.options[idx]
    return None


class QuizOut(Strict):
    questions: list[Question]

    @model_validator(mode="after")
    def _check(self):
        if not 3 <= len(self.questions) <= 12:
            raise ValueError("quiz must have 3-12 questions")
        ids = [q.id for q in self.questions]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate question ids")
        return self


# ------------------------------------------------------------------- Diagnoser
class DiagnoserOut(Strict):
    question_id: str
    result: Literal["correct", "partial", "wrong"]
    misconception: Optional[str] = None
    confidence: float = 0.5
    hint: str = ""

    @model_validator(mode="after")
    def _check(self):
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence out of range")
        return self


# -------------------------------------------------------------------- Rewriter
class ReviewInsert(Strict):
    after: str            # id of the chapter the review item follows
    title: str
    summary: str = ""
    objectives: list[str] = Field(default_factory=list)


class ChapterPatch(Strict):
    insert: list[ReviewInsert] = Field(default_factory=list)


class RewriterOut(Strict):
    patch: ChapterPatch
    reason: str = ""

    @model_validator(mode="after")
    def _check(self):
        if len(self.patch.insert) > MAX_INSERTS_PER_SESSION:
            raise ValueError(f"at most {MAX_INSERTS_PER_SESSION} review items per session")
        return self


# ------------------------------------------------------------------- Companion
class MemoryUpdates(BaseModel):
    model_config = ConfigDict(extra="forbid")  # only allowed fields

    mistake_labels_add: list[str] = Field(default_factory=list)
    last_chapter: Optional[str] = None
    notes: Optional[str] = None


class CompanionOut(Strict):
    message: str
    suggested_next: Optional[str] = None
    memory_updates: MemoryUpdates = Field(default_factory=MemoryUpdates)

    @field_validator("message")
    @classmethod
    def _len(cls, v):
        if not v.strip():
            raise ValueError("empty message")
        if len(v) > MAX_MESSAGE_CHARS:
            raise ValueError("message over length cap")
        return v


# ----------------------------------------------------------------------- Doubt
class DoubtOut(Strict):
    answer: str
    source_refs: list[str] = Field(default_factory=list)
    covered: bool = True                  # False: the notebook's material does not answer this (the engine may then look it up online)
    quote: str = ""                       # the exact source sentence the answer rests on (checked in code: dropped if it is not really in a source)

    @field_validator("answer")
    @classmethod
    def _nonempty(cls, v):
        if not v.strip():
            raise ValueError("empty answer")
        return v


# --------------------------------------------------------------------- profile
class LearnerProfile(Strict):
    learner_id: str
    language: str = "en"
    level: str = "beginner"
    goal: Optional[str] = None
    mistake_labels: list[str] = Field(default_factory=list)
