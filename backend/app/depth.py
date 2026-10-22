"""Depth profiles: one setting every content agent obeys (fixes thin chapters). Plain data and code.

The Teacher is asked for these sizes, the Quizzer and slide maker read the same numbers, and a section that comes back under its minimum triggers ONE
expansion pass (more examples, an edge case, a common mistake). Defaults are starting points to tune against eval/baseline.
"""
from __future__ import annotations

import os
import re
from typing import Optional

PROFILES = {
    "quick": {"words": 500, "examples": 1, "slides": 6, "quiz": 5},
    "standard": {"words": 1000, "examples": 2, "slides": 10, "quiz": 8},
    "deep": {"words": 1800, "examples": 3, "slides": 14, "quiz": 12},
}
DEFAULT = "standard"
EXPAND_BELOW = 0.6            # an expansion pass when the notes have fewer than this share of the target words


def name_for(store, nid: Optional[str]) -> str:
    """The notebook's depth (set by the Scoper or the learner), else AASAAN_DEPTH, else standard."""
    v = (store.cache_get(f"depth:{nid}") if store and nid else None) or os.environ.get("AASAAN_DEPTH") or DEFAULT
    return v if v in PROFILES else DEFAULT


def set_depth(store, nid: str, name: str) -> str:
    if name not in PROFILES:
        raise ValueError(f"depth must be one of {', '.join(PROFILES)}")
    store.cache_set(f"depth:{nid}", name)
    return name


def profile(store, nid: Optional[str]) -> dict:
    n = name_for(store, nid)
    return {"name": n, **PROFILES[n]}


def word_count(md: str) -> int:
    return len(re.findall(r"[^\W_]+", re.sub(r"```.*?```", " ", md or "", flags=re.S), re.UNICODE))


def needs_expansion(notes_md: str, prof: dict) -> bool:
    return word_count(notes_md) < EXPAND_BELOW * prof["words"]
