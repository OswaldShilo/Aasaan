"""Helpers shared by all agents."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

LANG_NAMES = {"en": "English", "hi": "Hindi", "kn": "Kannada", "ta": "Tamil"}

RULES = (
    "Rules: reply with ONLY one JSON object, no prose, no markdown fences. "
    "Use only facts from the student's material. Internal field names stay in English. "
    "Keep outputs short."
)


def lang_name(code: str) -> str:
    return LANG_NAMES.get(code, code)


def build_prompt(job: str, schema_example: dict, inputs: dict[str, Any], extra: str = "") -> str:
    return (
        f"{job}\n\n{RULES}\n{extra}\n\n"
        f"Output JSON schema (example):\n{json.dumps(schema_example, ensure_ascii=False)}\n\n"
        f"Inputs:\n{json.dumps(inputs, ensure_ascii=False)}"
    )


STOP = frozenset("the and for are but not you your with that this from have has was were what when where which who why how can could would should does did "
                 "about into than then them they their there here its it's isn't aren't also just like some any all one two more most very".split())
MATH_SYMBOLS = frozenset("\u2211\u220f\u222b\u221a\u221e\u2264\u2265\u2260\u2248\u2192\u2190\u2208\u2209\u222a\u2229\u2202\u2207\u00b1\u00d7\u00f7\u2200\u2203\u2282\u2286\u2261")


def tokens(text: str, min_len: int = 2) -> set[str]:
    """Words for matching a question against sources, in ANY script. Letters and combining marks (Indic vowel signs) stay together, so Tamil or
    Hindi words are whole tokens; math symbols (a sum sign, an integral, <=) count as tokens too; digits are kept as numbers. `\\w{3,}` did none of this."""
    t = unicodedata.normalize("NFKC", (text or "").lower())
    out: set[str] = set()
    cur: list[str] = []

    def flush() -> None:
        w = "".join(cur)
        cur.clear()
        if len(w) >= min_len and w not in STOP and not (w.isdigit() and len(w) < 2):
            out.add(w)
    for ch in t:
        if ch.isalnum() or unicodedata.category(ch).startswith("M"):
            cur.append(ch)
        else:
            flush()
            if ch in MATH_SYMBOLS:
                out.add(ch)
    flush()
    return out


def overlap(question_tokens: set[str], text: str) -> int:
    return len(question_tokens & tokens(text))


def split_sentences(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]


def ref_of(chunk: dict) -> dict:
    return {"source_id": chunk["chunk_id"].split("-")[0], "page": chunk["page"], "chunk_id": chunk["chunk_id"]}


def chunk_ids(chunks: list[dict]) -> set[str]:
    return {c["chunk_id"] for c in chunks}


def fingerprint(chunks: list[dict]) -> str:
    """Content hash of the source text a node is taught from. Cache keys use this (not the random
    source id) so a pre-warmed demo cache still hits when the same material is uploaded again."""
    h = hashlib.sha1("\n".join(c["text"].strip() for c in chunks).encode("utf-8")).hexdigest()
    return h[:16]
