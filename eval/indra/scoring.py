"""Ground truth and scoring for the Indra spike. Both extractors (the current path and Indra) are scored by the same code.

A tool's raw output is {"pages": [{"text": str, "markdown": str | None, "blocks": [{"label": str, "text": str}] | None}], "seconds": float}.
Scores:
  recall        share of the ground-truth phrases found (fuzzy, so one wrong character does not zero a phrase)
  table_rows    share of the table rows whose cells all land on ONE line / one markdown row (table fidelity)
  headings      share of the ground-truth headings found as a heading (markdown "#" or a Section-Header block); None when the tool has no structure
  checklist     share of contents rows recovered as separate items (list items or lines)
  coverage      share of pages with at least 20 characters of text
"""
from __future__ import annotations

import difflib
import re
import unicodedata

TEXT_PHRASES = ["Introduction to Data Structures", "Table of Contents", "Arrays and memory", "Linked lists", "Stacks and queues", "Trees", "Hash tables",
                "contiguous memory", "constant time", "Access by index", "Insert at front", "class Node", "self.next = None"]
TEXT_TABLE = [("Access by index", "O(1)", "O(n)"), ("Insert at front", "O(n)", "O(1)"), ("Search", "O(n)", "O(n)")]
TEXT_HEADINGS = ["Introduction to Data Structures", "Table of Contents", "Arrays and memory", "Linked lists", "Cost of common operations"]
TEXT_CHECKLIST = ["Arrays and memory", "Linked lists", "Stacks and queues", "Trees", "Hash tables"]
GROUND_TRUTH = {
    "text.pdf": {"phrases": TEXT_PHRASES, "table": TEXT_TABLE, "headings": TEXT_HEADINGS, "checklist": TEXT_CHECKLIST, "pages": 3},
    "scan.pdf": {"phrases": TEXT_PHRASES, "table": TEXT_TABLE, "headings": TEXT_HEADINGS, "checklist": TEXT_CHECKLIST, "pages": 3},
    "notes_photo.png": {"phrases": ["Binary search", "Sorted array only", "low = 0", "high = n - 1", "mid = (low + high) // 2", "a[mid] == x", "O(log n)"], "pages": 1},
    "hindi_scan.png": {"phrases": ["पाइथन में लूप", "लूप कई बार कोड दोहराता है", "दो प्रकार हैं"], "pages": 1},
    "tamil_scan.pdf": {"phrases": ["பைத்தானில் லூப்", "லூப் குறியீட்டை பல முறை", "இரண்டு வகைகள்"], "pages": 1},
    "cloud_de.pdf": {"phrases": [], "pages": 20},
}


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", s or "")).strip().lower()


def found(phrase: str, text: str, threshold: float = 0.82) -> bool:
    p, t = norm(phrase), norm(text)
    if not p:
        return True
    if p in t:
        return True
    n = len(p)
    best = 0.0
    step = max(1, n // 6)
    for i in range(0, max(1, len(t) - n // 2), step):          # a sliding window; cheap on page-sized text
        best = max(best, difflib.SequenceMatcher(None, p, t[i:i + n + n // 4]).ratio())
        if best >= threshold:
            return True
    return False


def _row_ok(cells: tuple[str, ...], lines: list[str]) -> bool:
    return any(all(found(c, ln, 0.9) for c in cells) for ln in lines)


def score(fixture: str, raw: dict) -> dict:
    gt = GROUND_TRUTH.get(fixture, {})
    pages = raw.get("pages") or []
    full = "\n".join((p.get("markdown") or p.get("text") or "") for p in pages)
    lines = [ln for ln in re.split(r"\n", full) if ln.strip()]
    out: dict = {"pages": len(pages), "seconds": raw.get("seconds"), "chars": len(full)}
    if raw.get("seconds") is not None and pages:
        out["seconds_per_page"] = round(raw["seconds"] / max(1, len(pages)), 3)
    out["coverage"] = round(sum(1 for p in pages if len((p.get("markdown") or p.get("text") or "").strip()) >= 20) / max(1, gt.get("pages", len(pages))), 2)
    if gt.get("phrases"):
        out["recall"] = round(sum(found(p, full) for p in gt["phrases"]) / len(gt["phrases"]), 2)
    if gt.get("table"):
        rows = lines + [" ".join(c.strip() for c in ln.split("|")) for ln in lines if "|" in ln]
        out["table_rows"] = round(sum(_row_ok(r, rows) for r in gt["table"]) / len(gt["table"]), 2)
    if gt.get("headings"):
        heads: list[str] = []
        for p in pages:
            heads += [re.sub(r"^#+\s*", "", ln).strip() for ln in (p.get("markdown") or "").splitlines() if ln.lstrip().startswith("#")]
            heads += [b.get("text", "") for b in (p.get("blocks") or []) if str(b.get("label", "")).lower().startswith("section-header")]
        structured = any(p.get("markdown") or p.get("blocks") for p in pages)
        out["headings"] = round(sum(any(found(h, x, 0.85) for x in heads) for h in gt["headings"]) / len(gt["headings"]), 2) if structured else None
    if gt.get("checklist"):
        items = [ln.strip() for ln in lines]
        out["checklist"] = round(sum(any(found(c, ln, 0.85) and len(ln) < len(c) + 40 for ln in items) for c in gt["checklist"]) / len(gt["checklist"]), 2)
    return out
