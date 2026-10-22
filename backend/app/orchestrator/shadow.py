"""Shadow mode: the real chapter build mirrored onto the board, bus and referee, so the new machinery runs against real work before it drives anything.

The old `_build_chapter` stays in charge (strangler). With AASAAN_ORCHESTRATOR=on (or per-notebook and opted in) the engine reports each step here:
an artifact per chapter output goes through its real lifecycle (ready -> claimed -> drafted -> checking -> approved), Critic -> Teacher revisions become
`Fixes` messages under the loop guards, and the referee releases in its fixed order. Nothing here changes what the learner sees; it is observed state
(the developer trace shows it) and the proof the board handles a real build. Plain code: no model, no router.
"""
from __future__ import annotations

import os
from typing import Optional

from .board import REFEREE, Board
from .bus import Bus, new_correlation_id
from .decisions import DecisionEmitter
from .manifests import load as load_manifests
from .referee import Referee


class Shadow:
    def __init__(self, store, emitter: DecisionEmitter):
        reg = load_manifests()
        self.board, self.bus = Board(store, reg), Bus(store, reg)
        self.referee = Referee(self.board, self.bus, emitter, reg)
        self.store = store
        self._ids: dict[tuple[int, str], int] = {}                   # (chapter id, artifact type) -> artifact id
        self._corr: dict[int, str] = {}

    def enabled(self, nid: str) -> bool:
        return self.referee.enabled(nid, {"AASAAN_ORCHESTRATOR": os.environ.get("AASAAN_ORCHESTRATOR", "off")})

    # -------------------------------------------------------------------------------- the build, as seen by the board
    def outline(self, nid: str, chapters: int) -> None:
        a = self.board.add(nid, "outline", owner_agent="architect", payload={"chapters": chapters}, priority=Referee.priority(1))
        self._walk(a, "architect", approve_by="referee")
        self.referee.release_pending(nid)

    def chapter_start(self, nid: str, cid: int, position: int, opened: bool = False) -> None:
        pri = Referee.priority(position, opened=opened)
        self._corr[cid] = new_correlation_id()
        notes = self.board.add(nid, "notes", chapter_id=cid, owner_agent="teacher", priority=pri)
        self._ids[(cid, "notes")] = notes
        self.board.claim("teacher", notebook_id=nid)

    def draft(self, nid: str, cid: int) -> None:
        a = self._ids.get((cid, "notes"))
        if a and (self.board.get(a) or {}).get("status") in ("claimed", "ready"):
            if self.board.get(a)["status"] == "ready":
                self.board.claim("teacher", notebook_id=nid)
            self.board.transition(a, "drafted", "teacher")
            self.board.transition(a, "checking", "critic")

    def fixes(self, nid: str, cid: int, issues: int) -> None:
        """The Critic sent claims back: a Fixes message (hop-limited) and a revision counted by the referee. The notes go back to the Teacher."""
        a = self._ids.get((cid, "notes"))
        if not a:
            return
        self.referee.send(nid, "critic", "teacher", "Fixes", correlation_id=self._corr.get(cid), artifact_id=a, payload={"issues": issues})
        if self.referee.allow_revision(a):
            self.board.transition(a, "drafted", REFEREE)

    def approved(self, nid: str, cid: int, verified: bool) -> None:
        a = self._ids.get((cid, "notes"))
        if not a:
            return
        st = self.board.get(a)["status"]
        if st == "drafted":
            self.board.transition(a, "checking", "critic")
        if not verified:
            self.board.set_flag(a, unverified=True)
        self.board.transition(a, "approved", "critic")
        for typ, owner in (("quiz", "quizzer"), ("slide_deck", "slides")):
            b = self.board.add(nid, typ, chapter_id=cid, owner_agent=owner, depends_on=[a], priority=Referee.priority(None, background=(typ == "slide_deck")))
            self._ids[(cid, typ)] = b
        self.referee.tick(nid)

    def outputs_ready(self, nid: str, cid: int) -> list[dict]:
        """Quiz and slides finished: they are approved and the referee releases them (slides only after the notes)."""
        for typ, owner in (("quiz", "quizzer"), ("slide_deck", "slides")):
            b = self._ids.get((cid, typ))
            if b:
                self.board.refresh_ready(nid)
                self.board.claim(owner, notebook_id=nid)
                self._walk(b, owner, approve_by="referee", claimed=True)
        return self.referee.release_pending(nid)

    # -------------------------------------------------------------------------------- helpers
    def _walk(self, aid: int, owner: str, approve_by: str = "referee", claimed: bool = False) -> None:
        a = self.board.get(aid)
        if a["status"] == "ready":
            self.board.claim(owner)
            a = self.board.get(aid)
        if a["status"] == "claimed":
            self.board.transition(aid, "drafted", owner)
        a = self.board.get(aid)
        if a["status"] == "drafted":
            self.board.transition(aid, "approved", REFEREE)

    def trace(self, nid: str) -> dict:
        arts = self.board.list(nid)
        return {"artifacts": [{k: a[k] for k in ("id", "type", "chapter_id", "status", "version", "owner_agent", "priority", "flags", "attempts")} for a in arts],
                "messages": [{k: m[k] for k in ("id", "correlation_id", "from_agent", "to_agent", "type", "artifact_id", "hop_count", "handled")}
                             for m in self.store._all("SELECT * FROM agent_messages WHERE notebook_id=? ORDER BY id", (nid,))]}
