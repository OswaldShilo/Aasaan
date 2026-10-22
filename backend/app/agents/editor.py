"""Editor: turns a student's request to change the course ("add a chapter on Delta Lake after chapter 5") into a small list of operations.

The model only PROPOSES operations. Code validates every one against the real course (positions exist, prerequisites still come
first, at least 3 and at most 24 chapters, no second operation on a chapter that is removed) and code writes the plain-language summary
the student sees, so a proposal can never describe something different from what Apply will do.
"""
from __future__ import annotations

from typing import Optional

from ..router import ModelRouter, RouterResult
from ..schemas import MAX_CHAPTERS_AFTER_PATCH, MIN_CHAPTERS
from .common import build_prompt

OPS = ("add_chapter", "remove_chapter", "move_chapter", "regenerate_chapter", "set_level", "set_scope")
SCOPE_DEPTHS = ("quick", "standard", "deep")
LEVELS = ("beginner", "intermediate", "advanced")
MAX_OPS = 4
EXAMPLE = {"ops": [
    {"op": "add_chapter", "after": 5, "title": "Delta Lake basics", "summary": "One or two sentences", "objectives": ["Explain what Delta Lake adds to a data lake"], "minutes": 12},
    {"op": "remove_chapter", "chapter": 3}, {"op": "move_chapter", "chapter": 4, "after": 1},
    {"op": "regenerate_chapter", "chapter": 2, "focus": "more worked examples"}, {"op": "set_level", "level": "intermediate"}]}


def _int(v, what: str) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a whole number")


def validate_ops(raw_ops: list, outline: list[dict], level: str) -> list[dict]:
    """Check proposed operations against the real course. `outline` = [{position, id, title, kind, prereq_ids}], in order.
    Returns normalised operations that name chapters by ID (positions shift; ids do not). Raises ValueError with a message that is fed back to
    the model for its one repair."""
    if not isinstance(raw_ops, list) or not 1 <= len(raw_ops) <= MAX_OPS:
        raise ValueError(f"propose between 1 and {MAX_OPS} operations")
    cur = [{"key": c["id"], "title": c["title"], "pre": set(c["prereq_ids"])} for c in outline]    # simulation of the course, by id
    orig = [c["id"] for c in outline]                                                              # positions in the request mean THESE chapters
    key_at = lambda pos: orig[pos - 1]  # noqa: E731
    idx_of = lambda key: next((i for i, c in enumerate(cur) if c["key"] == key), None)  # noqa: E731
    ops: list[dict] = []
    new_n = 0
    sim_level = level
    for o in raw_ops:
        if not isinstance(o, dict) or o.get("op") not in OPS:
            raise ValueError(f"each operation needs `op` to be one of {', '.join(OPS)}")
        kind, N = o["op"], len(orig)
        if kind == "add_chapter":
            after = _int(o.get("after", N), "after")
            title = str(o.get("title") or "").strip()
            objs = [str(x).strip() for x in (o.get("objectives") or []) if str(x).strip()][:4]
            if not 0 <= after <= N:
                raise ValueError(f"after must be between 0 and {N}")
            if not 3 <= len(title) <= 80 or not objs:
                raise ValueError("a new chapter needs a title (3 to 80 characters) and 1 to 4 objectives")
            if len(cur) + 1 > MAX_CHAPTERS_AFTER_PATCH:
                raise ValueError(f"a course can have at most {MAX_CHAPTERS_AFTER_PATCH} chapters")
            after_id = key_at(after) if after else None
            at = 0
            if after_id is not None:
                i = idx_of(after_id)
                if i is None:
                    raise ValueError(f"chapter {after} is removed by this same proposal")
                at = i + 1
            new_n += 1
            key = f"new{new_n}"
            cur.insert(at, {"key": key, "title": title, "pre": {after_id} if after_id else set()})
            ops.append({"op": "add_chapter", "after_id": after_id, "title": title[:80], "summary": str(o.get("summary") or "")[:240], "objectives": objs,
                        "minutes": max(5, min(60, _int(o.get("minutes", 12), "minutes"))), "key": key,
                        **({"review_for": str(o["review_for"])[:80], "session": str(o.get("session") or "")[:64]} if o.get("review_for") else {})})
        elif kind in ("remove_chapter", "move_chapter", "regenerate_chapter"):
            pos = _int(o.get("chapter"), "chapter")
            if not 1 <= pos <= N:
                raise ValueError(f"chapter must be between 1 and {N}")
            key = key_at(pos)
            i = idx_of(key)
            if i is None:
                raise ValueError(f"chapter {pos} is already removed")
            ch = cur[i]
            if kind == "remove_chapter":
                if len(cur) - 1 < MIN_CHAPTERS:
                    raise ValueError(f"a course needs at least {MIN_CHAPTERS} chapters")
                for other in cur:
                    if key in other["pre"]:
                        other["pre"] = (other["pre"] - {key}) | ch["pre"]
                cur.pop(i)
                ops.append({"op": "remove_chapter", "chapter_id": key})
            elif kind == "move_chapter":
                after = _int(o.get("after"), "after")
                if not 0 <= after <= N or after in (pos, pos - 1):
                    raise ValueError("after must be a different position that actually moves the chapter")
                target = key_at(after) if after else None
                if target is not None and idx_of(target) is None:
                    raise ValueError(f"chapter {after} is removed by this same proposal")
                item = cur.pop(i)
                cur.insert(0 if target is None else idx_of(target) + 1, item)
                ops.append({"op": "move_chapter", "chapter_id": key, "after_id": target})
            else:
                ops.append({"op": "regenerate_chapter", "chapter_id": key, "focus": str(o.get("focus") or "").strip()[:120]})
        elif kind == "set_scope":        # built by code (the Scoper), never proposed by the Editor model; `affected_ids` are the chapters it would rewrite
            depth, lv = o.get("depth"), o.get("level")
            if depth is not None and depth not in SCOPE_DEPTHS:
                raise ValueError(f"depth must be one of {', '.join(SCOPE_DEPTHS)}")
            if lv is not None and lv not in LEVELS:
                raise ValueError(f"level must be one of {', '.join(LEVELS)}")
            if depth is None and lv is None:
                raise ValueError("a scope change needs a depth or a level")
            valid = {c["id"] for c in outline}
            ops.append({"op": "set_scope", "depth": depth, "level": lv, "affected_ids": [i for i in (o.get("affected_ids") or []) if i in valid], "minutes": int(o.get("minutes") or 0)})
        else:  # set_level
            lv = str(o.get("level") or "").lower()
            if lv not in LEVELS:
                raise ValueError(f"level must be one of {', '.join(LEVELS)}")
            if lv == sim_level:
                raise ValueError(f"the level is already {lv}")
            sim_level = lv
            ops.append({"op": "set_level", "level": lv})
    # prerequisites must still come first, whatever was moved, added or removed
    order = {c["key"]: i for i, c in enumerate(cur)}
    for i, c in enumerate(cur):
        for p in c["pre"]:
            if p in order and order[p] >= i:
                raise ValueError(f"'{c['title']}' would come before a chapter it depends on")
    return ops


def describe(op: dict, outline: list[dict]) -> str:
    """Fixed, code-written wording for one operation."""
    pos = {c["id"]: c["position"] for c in outline}
    title = {c["id"]: c["title"] for c in outline}
    if op["op"] == "add_chapter":
        a = pos.get(op["after_id"], 0)
        return f'Add a chapter on "{op["title"]}" {"after chapter " + str(a) if a else "at the start"} (about {op["minutes"]} min)'
    if op["op"] == "remove_chapter":
        return f'Remove chapter {pos.get(op["chapter_id"], "?")}: "{title.get(op["chapter_id"], "")[:50]}"'
    if op["op"] == "move_chapter":
        a = pos.get(op["after_id"], 0)
        return f'Move chapter {pos.get(op["chapter_id"], "?")} "{title.get(op["chapter_id"], "")[:40]}" {"after chapter " + str(a) if a else "to the start"}'
    if op["op"] == "regenerate_chapter":
        return f'Rewrite chapter {pos.get(op["chapter_id"], "?")} "{title.get(op["chapter_id"], "")[:40]}"' + (f' with a focus on {op["focus"]}' if op.get("focus") else "")
    if op["op"] == "set_scope":
        bits = [b for b in (f"depth to {op['depth']}" if op.get("depth") else "", f"level to {op['level']}" if op.get("level") else "") if b]
        n = len(op.get("affected_ids") or [])
        return f"Change the {' and '.join(bits)}." + (f" {n} chapter{'s' if n != 1 else ''} you have not started would be rewritten (about {op.get('minutes', 0)} min). The rest stay as they are." if n else " Chapters yet to be written will follow it.")
    return f'Change the level to {op["level"]}'


def run(router: ModelRouter, request: str, outline: list[dict], level: str, resources: list[str], profile) -> RouterResult:
    ctx = {"request": request, "outline": outline, "level": level, "resources": resources}

    def validate(raw: dict) -> list[dict]:
        ops = raw.get("ops") if isinstance(raw, dict) else None
        if ops == []:
            return []        # the model says no change is needed or possible: the student is told so
        return validate_ops(ops, outline, level)

    prompt = build_prompt(
        "You are Editor. The student wants to change their course. Turn the request into 1 to 4 operations on the course outline, "
        "using chapter POSITIONS (1 is the first chapter). Operations: add_chapter (after = position to insert after, 0 = at the start), "
        "remove_chapter, move_chapter (after = position to move it after), regenerate_chapter (optional focus), set_level. "
        "Positions always mean the chapters as they are NOW, before any operation in your list. Do not invent resources. If the request cannot be done as an edit (it is a question, or unclear), reply with {\"ops\": []}.",
        EXAMPLE, {"request": request, "current_level": level, "resources_the_student_added": resources[:6],
                  "chapters": [{"position": c["position"], "title": c["title"], "summary": c["summary"][:120], "kind": c["kind"]} for c in outline]})
    return router.call("editor", prompt, ctx, validate, last_resort=lambda c: [], summary="Edit proposal", language=profile.language)
