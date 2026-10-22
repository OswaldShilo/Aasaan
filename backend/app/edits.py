"""Course edits as PROPOSALS: the Editor suggests, the student applies, and every applied change can be undone.

  propose   a request (or a newly added resource) becomes a stored proposal and a card in the chat. Nothing changes yet.
  apply     re-checks the proposal against the course as it is NOW, snapshots what it will touch, then makes the change.
  undo      puts everything back (last-in-first-out, at least the last 10 changes). A chapter being rebuilt right now blocks the undo.
  dismiss   forgets a pending proposal.

Edits to a chapter regenerate only that chapter, through the normal build (Teacher, Critic, Quiz, Illustrator).
"""
from __future__ import annotations

import time
from typing import Optional

from . import depth as depth_mod
from .agents import editor
from .errors import UserError

KEEP_UNDO = 10          # the most recent applied proposals keep their snapshots
GEN_BUSY = ("drafting", "checking")


def outline_of(store, nid: str) -> list[dict]:
    chs = store.chapters(nid)
    return [{"position": c["position"], "id": c["id"], "title": c["title"], "summary": c["summary"], "kind": c["kind"],
             "prereq_ids": [p for p in c["prereqs"] if any(x["id"] == p for x in chs)]} for c in chs]


def view(store, p: dict) -> dict:
    """What the client needs to draw the card: fixed wording, per-operation lines, and what the buttons may do."""
    outline = outline_of(store, p["notebook_id"])
    latest = next((x for x in store.proposals(p["notebook_id"], 50) if x["status"] == "applied"), None)
    return {"id": p["id"], "status": p["status"], "source": p["source"], "summary": p["summary"],
            "lines": [editor.describe(o, outline) if p["status"] == "pending" else (o.get("_line") or editor.describe(o, outline)) for o in p["ops"]],
            "can_undo": p["status"] == "applied" and bool(p.get("undo")) and latest is not None and latest["id"] == p["id"]}


def _summary(lines: list[str]) -> str:
    return lines[0] if len(lines) == 1 else f"{len(lines)} changes: " + "; ".join(lines)


def propose(engine, nid: str, learner_id: str, request: str, source: str = "chat", resources: Optional[list[str]] = None) -> Optional[dict]:
    """Ask the Editor, validate, store, and post the proposal card. None when there is nothing to propose (the chat says so)."""
    nb = engine._nb(nid)
    prof = engine.profile(learner_id, nb)
    outline = outline_of(engine.store, nid)
    with engine._scope(nid):
        ops = editor.run(engine.router, request, outline, nb["level"] or "beginner", resources or [s["title"] for s in engine.store.sources(nid)], prof).value
    if not ops:
        return None
    lines = [editor.describe(o, outline) for o in ops]
    for o, line in zip(ops, lines):
        o["_line"] = line                                   # the wording shown now stays with the proposal, even after chapters move
    pid = engine.store.add_proposal(nid, source, _summary(lines), ops)
    mid = engine.store.add_message(nid, "assistant", _summary(lines), kind="proposal", refs=[{"kind": "proposal", "id": pid}])
    return {**view(engine.store, engine.store.get_proposal(pid)), "message_id": mid}


def _post(engine, nid: str, source: str, ops: list[dict]) -> dict:
    """Store validated ops as a pending proposal with its fixed wording, and put the card in the chat."""
    outline = outline_of(engine.store, nid)
    lines = [editor.describe(o, outline) for o in ops]
    for o, line in zip(ops, lines):
        o["_line"] = line
    pid = engine.store.add_proposal(nid, source, _summary(lines), ops)
    mid = engine.store.add_message(nid, "assistant", _summary(lines), kind="proposal", refs=[{"kind": "proposal", "id": pid}])
    return {**view(engine.store, engine.store.get_proposal(pid)), "message_id": mid}


def propose_scope(engine, nid: str, depth: Optional[str] = None, level: Optional[str] = None) -> Optional[dict]:
    """The Scoper's change of depth or level as a proposal that lists what would be rewritten and roughly how long it takes. Never applied silently."""
    store, nb = engine.store, engine._nb(nid)
    cur_depth, cur_level = depth_mod.name_for(store, nid), nb["level"] or "beginner"
    depth = depth if depth and depth != cur_depth else None
    level = level if level and level != cur_level else None
    if not depth and not level:
        return None
    chs = store.chapters(nid)
    affected = [c["id"] for c in chs if c["status"] == "ready" and not c["started"] and not c["completed"]]
    ops = editor.validate_ops([{"op": "set_scope", "depth": depth, "level": level, "affected_ids": affected, "minutes": round(1.5 * len(affected))}], outline_of(store, nid), cur_level)
    return _post(engine, nid, "scope", ops)


def propose_review(engine, nid: str, session_id: str, after_chapter_id: int, inserts: list[dict], misconception: str) -> Optional[dict]:
    """The Rewriter's review chapters, as a proposal: nothing is added until the learner applies it, and Undo removes it again."""
    outline = outline_of(engine.store, nid)
    pos = {c["id"]: c["position"] for c in outline}
    raw = []
    for ins in inserts:
        after = pos.get(int(ins["after"]), pos.get(after_chapter_id, 0))
        raw.append({"op": "add_chapter", "after": after, "title": ins["title"], "summary": ins.get("summary", ""), "objectives": ins.get("objectives", []), "minutes": 8, "review_for": misconception, "session": session_id})
    try:
        ops = editor.validate_ops(raw, outline, engine._nb(nid)["level"] or "beginner")
    except ValueError:
        return None
    return _post(engine, nid, "rewriter", ops)


def _live_ops(engine, nid: str, ops: list[dict]) -> list[dict]:
    """Re-validate against the course as it is now (it may have changed since the proposal was made)."""
    outline = outline_of(engine.store, nid)
    pos = {c["id"]: c["position"] for c in outline}
    raw = []
    for o in ops:
        for k in ("chapter_id", "after_id"):
            if o.get(k) and o[k] not in pos:
                raise UserError("The course changed since this was proposed, so I can't apply it safely. Ask me again and I'll make a fresh proposal.")
        if o["op"] == "add_chapter":
            raw.append({"op": "add_chapter", "after": pos.get(o["after_id"], 0), "title": o["title"], "summary": o.get("summary", ""), "objectives": o["objectives"], "minutes": o.get("minutes", 12),
                        **({"review_for": o["review_for"], "session": o.get("session", "")} if o.get("review_for") else {})})
        elif o["op"] in ("remove_chapter", "regenerate_chapter"):
            raw.append({"op": o["op"], "chapter": pos[o["chapter_id"]], **({"focus": o.get("focus", "")} if o["op"] == "regenerate_chapter" else {})})
        elif o["op"] == "move_chapter":
            raw.append({"op": "move_chapter", "chapter": pos[o["chapter_id"]], "after": pos.get(o["after_id"], 0)})
        else:
            raw.append(o)
    try:
        return editor.validate_ops(raw, outline, engine._nb(nid)["level"] or "beginner")
    except ValueError as e:
        raise UserError(f"That change no longer fits the course: {e}.")


def apply(engine, nid: str, pid: int) -> dict:
    store = engine.store
    p = store.get_proposal(pid)
    if not p or p["notebook_id"] != nid:
        raise UserError("unknown proposal")
    if p["status"] != "pending":
        raise UserError("This proposal was already handled.")
    ops = _live_ops(engine, nid, p["ops"])
    before = store.chapters(nid)
    undo = {"order": [c["id"] for c in before], "prereqs": {str(c["id"]): c["prereqs"] for c in before}, "level": engine._nb(nid)["level"],
            "added": [], "removed": [], "regenerated": []}
    touched: list[int] = []
    keymap: dict[str, int] = {}
    for o in ops:
        order = [c["id"] for c in store.chapters(nid)]
        if o["op"] == "add_chapter":
            after = o["after_id"] if o["after_id"] is None else keymap.get(o["after_id"], o["after_id"])
            at = 0 if after is None else order.index(after) + 1
            store.shift_positions(nid, at, 1)
            summary = o["summary"] or "Added at your request."
            cid = store.add_chapter(nid, at + 1, o["title"], summary, o["objectives"], [after] if after else [], [], kind="review" if o.get("review_for") else "core", priority=80 if o.get("review_for") else 70,
                                    meta={"added_by": pid, "minutes": o["minutes"], **({"misconception": o["review_for"], "session": o.get("session", "")} if o.get("review_for") else {})})
            store.add_job(nid, cid, "chapter")
            keymap[o["key"]] = cid
            undo["added"].append(cid)
            touched.append(cid)
        elif o["op"] == "remove_chapter":
            cid = o["chapter_id"]
            snap = store.chapter_snapshot(cid)
            store.add_revision(nid, cid, pid, "removed", snap)
            for other in store.chapters(nid):
                if cid in other["prereqs"]:
                    store.set_chapter_prereqs(other["id"], [x for x in other["prereqs"] if x != cid] + [x for x in snap["chapter"]["prereqs"] if x not in other["prereqs"]])
                    touched.append(other["id"])
            store.delete_chapter(cid)
            store.set_positions([x for x in order if x != cid])
            undo["removed"].append(cid)
        elif o["op"] == "move_chapter":
            cid, after = o["chapter_id"], o["after_id"]
            order.remove(cid)
            order.insert(0 if after is None else order.index(after) + 1, cid)
            store.set_positions(order)
            touched.append(cid)
        elif o["op"] == "regenerate_chapter":
            cid = o["chapter_id"]
            store.add_revision(nid, cid, pid, "regenerated", store.chapter_snapshot(cid))
            ch = store.get_chapter(cid)
            objectives = ch["objectives"] + ([f"Focus: {o['focus']}"] if o.get("focus") else [])
            store.update_chapter(cid, objectives=objectives, priority=70, meta={**ch["meta"], "regenerated_by": pid})
            store.set_chapter_status(cid, "queued")
            store.add_job(nid, cid, "chapter")
            undo["regenerated"].append(cid)
            touched.append(cid)
        elif o["op"] == "set_level":
            store.update_notebook(nid, level=o["level"])
        elif o["op"] == "set_scope":
            undo["depth"] = depth_mod.name_for(store, nid)
            if o.get("depth"):
                depth_mod.set_depth(store, nid, o["depth"])
            if o.get("level"):
                store.update_notebook(nid, level=o["level"])
            for cid in o.get("affected_ids", []):                       # only chapters that are ready and not started are rewritten
                ch = store.get_chapter(cid)
                if not ch or ch["status"] != "ready" or ch["started"] or ch["completed"]:
                    continue
                store.add_revision(nid, cid, pid, "regenerated", store.chapter_snapshot(cid))
                store.update_chapter(cid, priority=60, meta={**ch["meta"], "regenerated_by": pid})
                store.set_chapter_status(cid, "queued")
                store.add_job(nid, cid, "chapter")
                undo["regenerated"].append(cid)
                touched.append(cid)
    engine._assert_acyclic(nid)
    store.update_proposal(pid, status="applied", undo=undo, touched=touched, applied_at=time.time(), ops=ops_with_lines(p["ops"], ops))
    _prune(store, nid)
    engine.say(nid, f"Applied: {p['summary']}", key=f"applied:{pid}", refs=[{"kind": "chapter", "id": c, "label": (store.get_chapter(c) or {}).get("title", "")[:50]} for c in touched[:3] if store.get_chapter(c)])
    engine._refresh_status(nid)
    return view(store, store.get_proposal(pid))


def ops_with_lines(old: list[dict], new: list[dict]) -> list[dict]:
    """Keep the wording the student saw on each applied operation."""
    return [{**n, "_line": (old[i].get("_line") if i < len(old) else None)} for i, n in enumerate(new)]


def _prune(store, nid: str) -> None:
    applied = [x for x in store.proposals(nid, 100) if x["status"] == "applied" and x.get("undo")]
    for old in applied[KEEP_UNDO:]:
        store.update_proposal(old["id"], undo=None)          # too old to undo: its snapshots are dropped
        store.delete_revisions(old["id"])


def undo(engine, nid: str, pid: int) -> dict:
    store = engine.store
    p = store.get_proposal(pid)
    if not p or p["notebook_id"] != nid:
        raise UserError("unknown proposal")
    v = view(store, p)
    if not v["can_undo"]:
        raise UserError("Only the most recent change can be undone, and only if it is still recent enough.")
    u = p["undo"]
    busy = [c for c in u["regenerated"] + u["added"] if (store.get_chapter(c) or {}).get("status") in GEN_BUSY]
    if busy:
        raise UserError("A chapter from this change is being written right now. Try undoing again in a minute.")
    revs = store.revisions(pid)
    for cid in u["added"]:                                    # chapters this proposal created go away
        store.cancel_chapter_jobs(cid)
        store.delete_chapter(cid)
    for r in revs:
        if r["kind"] == "removed":
            store.restore_chapter(r["snapshot"])
        elif r["kind"] == "regenerated":
            snap = r["snapshot"]
            c = snap["chapter"]
            store.cancel_chapter_jobs(c["id"])
            store.update_chapter(c["id"], objectives=c["objectives"], meta=c["meta"], priority=c.get("priority", 0))
            store.replace_blocks(c["id"], snap["blocks"], c["status"], c["meta"])
    alive = {c["id"] for c in store.chapters(nid)}
    for cid_s, pre in u["prereqs"].items():
        if int(cid_s) in alive:
            store.set_chapter_prereqs(int(cid_s), [x for x in pre if x in alive])
    store.set_positions([c for c in u["order"] if c in alive])
    store.update_notebook(nid, level=u["level"])
    if u.get("depth"):
        depth_mod.set_depth(store, nid, u["depth"])
    touched = [c for c in p["touched"] if c in alive] + u["removed"]
    store.update_proposal(pid, status="undone", undone_at=time.time(), touched=touched)
    store.delete_revisions(pid)
    engine._assert_acyclic(nid)
    engine.say(nid, f"Undone: {p['summary']}", key=f"undone:{pid}")
    engine._refresh_status(nid)
    return view(store, store.get_proposal(pid))


def dismiss(engine, nid: str, pid: int) -> dict:
    p = engine.store.get_proposal(pid)
    if not p or p["notebook_id"] != nid:
        raise UserError("unknown proposal")
    if p["status"] == "pending":
        engine.store.update_proposal(pid, status="dismissed")
    return view(engine.store, engine.store.get_proposal(pid))


def snapshot_info(store, nid: str) -> dict:
    """For the notebook snapshot: card states and the chapters to flash for about two seconds after an apply or undo."""
    ps = store.proposals(nid, 10)
    recent = next((p for p in ps if p["status"] in ("applied", "undone") and time.time() - (p.get("undone_at") or p.get("applied_at") or 0) < 8), None)
    latest = next((p for p in ps if p["status"] == "applied" and p.get("undo")), None)
    return {"proposals": [{"id": p["id"], "status": p["status"], "can_undo": bool(latest and latest["id"] == p["id"] and p["status"] == "applied")} for p in ps],
            "highlight": {"key": f"{recent['id']}:{recent['status']}", "ids": recent["touched"]} if recent else None}
