"""The Intake contract: a validated `source_pack`, the only thing the Architect reads.

Intake turns what the learner gave (documents, photos, links, pasted text, the goal sentence) into this clean pack. It does NOT plan chapters,
search for resources, or write explanations. Plain data: no model calls here.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


Status = Literal["readable", "partly_readable", "unreadable"]
Tier = Literal["text", "text-layer", "indra-auto", "indra-ocr", "vision", "raw-text", "none"]
ChunkKind = Literal["prose", "list", "code", "table_ref"]


class PageQuality(_M):
    """One group of pages that share a status and a reason (kept short: '12 pages have only grid lines')."""
    pages: list[int]
    status: Status
    reason: str = ""
    tier: Tier = "none"
    tried: list[str] = Field(default_factory=list)


class SourceInfo(_M):
    id: str
    kind: Literal["text", "markdown", "pdf", "image", "url"]
    name: str
    language: str = "en"
    pages: int = 0
    status: Status = "readable"
    quality_report: list[PageQuality] = Field(default_factory=list)


class PageInfo(_M):
    source_id: str
    page: int
    tier: Tier
    source_kind: str = ""                 # what the page really was: text layer, scan, photo, drawing, blank
    confidence: Optional[float] = None
    escalated_because: str = ""           # why the page left the cheap tier ("low confidence 0.55", "script not supported by OCR")


class OutlineHint(_M):
    text: str
    level: int
    source_id: str
    page: int


class Chunk(_M):
    id: str                               # s1-p12-c3: stable for the same input in the same order
    source_id: str
    page: int
    heading_path: list[str] = Field(default_factory=list)
    text: str
    kind: ChunkKind = "prose"


class Table(_M):
    id: str
    source_id: str
    page: int
    columns: list[str]
    rows: list[list[str]]
    caption: str = ""


class Figure(_M):
    id: str
    source_id: str
    page: int
    caption: str = ""
    ocr_text: str = ""
    image_ref: str = ""


class LinkFound(_M):
    url: str
    anchor_text: str = ""
    source_id: str
    page: int


class ChecklistItem(_M):
    text: str
    source_id: str
    page: int
    order: int


class Goal(_M):
    text: str = ""
    language: str = "en"


class Brief(_M):
    """Cheap hints only. The Scoper owns the final scope; the Architect never waits for it."""
    subject: str = ""
    language: str = "en"
    level_hint: Literal["beginner", "intermediate", "advanced", "unknown"] = "unknown"
    track_hint: Literal["competitive_programming", "jee", "neet", "boards", "general"] = "general"
    size: str = "small"                   # small (<= 10 pages), medium (<= 60), large
    looks_like_syllabus: bool = False


class SourcePack(_M):
    sources: list[SourceInfo]
    pages: list[PageInfo]
    outline_hints: list[OutlineHint] = Field(default_factory=list)
    chunks: list[Chunk] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    figures: list[Figure] = Field(default_factory=list)
    links_found: list[LinkFound] = Field(default_factory=list)
    checklist: list[ChecklistItem] = Field(default_factory=list)
    goal: Goal = Field(default_factory=Goal)
    brief: Brief = Field(default_factory=Brief)
    markdown: dict[str, str] = Field(default_factory=dict)    # source id -> clean Markdown, for the Resources view
