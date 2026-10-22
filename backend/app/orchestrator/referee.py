"""The referee: a thin piece of plain code that enforces the rules. It never writes content and never decides what an agent says.

What it does: priority tags, per-notebook and global budgets, per-role concurrency (through claims), loop limits (Critic -> Teacher
revisions, message hops), lease recovery, deadlock and stuck-work detection, and the ONLY release of artifacts, in a fixed order.
It makes no model calls and must not import the model router (a test enforces this); it only talks to the board, the bus and the emitter.
"""
from __future__ import annotations

import os
import threading
from collections import defaultdict
from typing import Any, Optional

from .board import REFEREE, Board, BoardError
from .bus import Bus, LoopStopped
from .decisions import DecisionEmitter
from .manifests import Registry

MAX_REVISIONS = 2                                   # Critic -> Teacher loops per artifact
PRIORITY = {"opened": 0, "first": 10, "next": 20, "prefetch": 60, "background": 80}   # lower number = served first
RELEASE_ORDER = ("outline", "notes", "video", "links", "images", "diagram", "code_flowchart", "practice", "quiz", "slide_deck", "summary")
INTERNAL_TYPES = ("source_pack", "scope", "resource_pack", "mastery")      # live on the board, never shown to the learner
DIRECT_TYPES = ("video", "links", "images", "diagram", "code_flowchart", "practice", "quiz", "summary")   # released the moment they are approved
NO_AUTO_RELEASE = ("proposal",)                                  # proposals always wait for the learner (Apply / Undo)
KILL_VALUES = ("off", "0", "false", "no", "")


def orchestrator_mode(env: Optional[dict] = None) -> str:
    """AASAAN_ORCHESTRATOR: `off` (default, the old pipeline runs), `on` (every notebook), `per-notebook` (only notebooks that opted in)."""
    v = ((env if env is not None else os.environ).get("AASAAN_ORCHESTRATOR", "off") or "off").strip().lower()
    return "off" if v in KILL_VALUES else ("per-notebook" if v.startswith("per") else "on")


class Budgets:
    """Model and tool call budgets per notebook, and in total. `charge` returns False once a budget is spent."""
    def __init__(self, per_notebook: int = 400, total: int = 4000):
        self.per_notebook, self.total = per_notebook, total
        self._n: dict[str, int] = defaultdict(int)
        self._all = 0
        self._lock = threading.Lock()

    def charge(self, notebook_id: str, calls: int = 1) -> bool:
        with self._lock:
            if self._n[notebook_id] + calls > self.per_notebook or self._all + calls > self.total:
                return False
            self._n[notebook_id] += calls
            self._all += calls
            return True

    def used(self, notebook_id: str) -> int:
        with self._lock:
            return self._n.get(notebook_id, 0)


class Referee:
    def __init__(self, board: Board, bus: Bus, emitter: DecisionEmitter, registry: Registry, budgets: Optional[Budgets] = None):
        self.board, self.bus, self.emit, self.registry = board, bus, emitter, registry
        self.store = board.store
        self.budgets = budgets or Budgets()

    # ------------------------------------------------------------------ kill switch and one-time fallback
    def enabled(self, notebook_id: str, env: Optional[dict] = None) -> bool:
        mode = orchestrator_mode(env)
        if mode == "per-notebook":
            return bool(self.store.cache_get(f"orch:nb:{notebook_id}"))
        return mode == "on"

    def opt_in(self, notebook_id: str, on: bool = True) -> None:
        self.store.cache_set(f"orch:nb:{notebook_id}", bool(on))

    def fallback_once(self, notebook_id: str, chapter_id: int, reason: str = "") -> bool:
        """An orchestrated chapter failed for good: allow ONE fallback to the old build path for it, and record that it happened."""
        key = f"orch:fallback:{chapter_id}"
        if self.store.cache_get(key):
            return False
        self.store.cache_set(key, True)
        self.emit.emit(notebook_id, REFEREE, "fell_back", what="this chapter's build", reason_code="tool_unavailable", reason_text=reason or "the new pipeline failed",
                       outcome="partial", severity="notice", chapter_id=chapter_id)
        return True

    # ------------------------------------------------------------------ priority
    @staticmethod
    def priority(position: Optional[int], *, opened: bool = False, background: bool = False, prefetch: bool = False) -> int:
        """opened chapter < chapter 1 < the next chapters in order < prefetch of the next one < background work."""
        if opened:
            return PRIORITY["opened"]
        if background:
            return PRIORITY["background"]
        if prefetch:
            return PRIORITY["prefetch"]
        if position is None or position <= 1:
            return PRIORITY["first"]
        return min(PRIORITY["prefetch"] - 1, PRIORITY["next"] + int(position))

    def bump_opened(self, notebook_id: str, chapter_id: int) -> int:
        """The learner opened a chapter: its unfinished work goes to the front of the queue."""
        with self.store.transaction(immediate=True):
            n = self.store.conn.execute(
                "UPDATE artifacts SET priority=? WHERE notebook_id=? AND chapter_id=? AND status IN ('blocked','ready') AND priority>?",
                (PRIORITY["opened"], notebook_id, chapter_id, PRIORITY["opened"])).rowcount
        if n:
            self.emit.emit(notebook_id, REFEREE, "chose", what="this chapter first", reason_code="user_opened", chapter_id=chapter_id,
                           dedupe_key=f"opened|{chapter_id}|{n}")
        return n

    # ------------------------------------------------------------------ budgets
    def charge(self, notebook_id: str, calls: int = 1) -> bool:
        ok = self.budgets.charge(notebook_id, calls)
        if not ok:
            self.emit.emit(notebook_id, REFEREE, "stopped", what="further model calls", reason_code="rate_limited", reason_text="the budget for this notebook is used",
                           outcome="partial", severity="warning", dedupe_key=f"budget|{notebook_id}")
        return ok

    # ------------------------------------------------------------------ loops
    def allow_revision(self, artifact_id: int) -> bool:
        """Critic -> Teacher: at most MAX_REVISIONS rounds, then the artifact ships as unverified."""
        a = self.board.get(artifact_id)
        if a is None:
            raise BoardError(f"artifact {artifact_id} does not exist")
        done = int(a["flags"].get("revisions", 0))
        if done >= MAX_REVISIONS:
            self.board.set_flag(artifact_id, unverified=True)
            self.emit.emit(a["notebook_id"], REFEREE, "stopped", what="a revision loop", reason_code="loop_limit", reason_text=f"after {MAX_REVISIONS} tries",
                           outcome="partial", severity="notice", chapter_id=a["chapter_id"], artifact_id=artifact_id)
            return False
        self.board.set_flag(artifact_id, revisions=done + 1)
        return True

    def send(self, notebook_id: str, frm: str, to: str, type: str, **kw: Any) -> Optional[dict]:
        """Send through the bus. A reply chain that hits the hop limit is stopped here and recorded, never raised into an agent."""
        try:
            return self.bus.send(notebook_id, frm, to, type, **kw)
        except LoopStopped as e:
            self.emit.emit(notebook_id, REFEREE, "stopped", what="an agent exchange", reason_code="loop_limit", outcome="partial", severity="notice",
                           correlation_id=kw.get("correlation_id"), artifact_id=kw.get("artifact_id"), dedupe_key=f"hops|{kw.get('correlation_id')}")
            return None

    # ------------------------------------------------------------------ release (the only place artifacts are released)
    def _releasable(self, a: dict, released: set[tuple[Optional[int], str]], nb_outline: bool) -> bool:
        t, st = a["type"], a["status"]
        if t in INTERNAL_TYPES or t in NO_AUTO_RELEASE:
            return False
        if t == "outline":
            return st == "approved"
        if not nb_outline:                       # nothing is released before the outline is
            return False
        if t == "notes":
            return st in ("drafted", "approved")                       # a labelled draft appears first, verification upgrades it in place
        if t == "slide_deck":
            return st == "approved" and (a["chapter_id"], "notes") in released   # slides never go ahead of the notes
        if t in DIRECT_TYPES:
            return st == "approved"
        return False

    def release_pending(self, notebook_id: str) -> list[dict]:
        """Release everything that is ready to be shown, in a fixed order. Returns the released artifacts."""
        arts = self.board.list(notebook_id)
        released = {(a["chapter_id"], a["type"]) for a in arts if a["status"] == "released"}
        nb_outline = any(a["type"] == "outline" and a["status"] == "released" for a in arts)
        order = {t: i for i, t in enumerate(RELEASE_ORDER)}
        out: list[dict] = []
        for a in sorted((x for x in arts if x["status"] in ("drafted", "approved")), key=lambda x: (order.get(x["type"], 99), x["priority"], x["id"])):
            if not self._releasable(a, released, nb_outline):
                continue
            draft = a["type"] == "notes" and a["status"] == "drafted"
            if self.board.transition(a["id"], "released", REFEREE):
                if draft:
                    self.board.set_flag(a["id"], draft=True)
                released.add((a["chapter_id"], a["type"]))
                if a["type"] == "outline":
                    nb_outline = True
                self.emit.emit(notebook_id, REFEREE, "released", what=f"{a['type'].replace('_', ' ')}{' (draft)' if draft else ''}", chapter_id=a["chapter_id"], artifact_id=a["id"],
                               dedupe_key=f"release|{a['id']}|{a['version']}")
                out.append({**a, "status": "released", "draft": draft})
        return out

    def upgrade_released(self, artifact_id: int, payload: Any, *, verified: bool = True, flags: Optional[dict] = None) -> None:
        """Verification finished (or fixes landed): patch the already released artifact in place and bump its version."""
        self.board.patch_released(artifact_id, payload, {"draft": False, "verified": verified, **(flags or {})}, actor=REFEREE)
        a = self.board.get(artifact_id)
        self.emit.emit(a["notebook_id"], REFEREE, "released", what=f"{a['type']} (verified)" if verified else f"{a['type']} (unverified)", chapter_id=a["chapter_id"],
                       artifact_id=artifact_id, severity="info" if verified else "notice", dedupe_key=f"upgrade|{artifact_id}|{a['version']}")

    # ------------------------------------------------------------------ stuck work and deadlock
    def detect_stuck(self, notebook_id: str) -> list[int]:
        """Blocked work that can never start (a dependency failed or was replaced, or nothing is runnable) is marked failed with a reason."""
        failed: list[int] = []
        self.board.refresh_ready(notebook_id)           # work whose dependencies are done is ready, not stuck
        for _ in range(1000):                           # each pass must fail something new; a pass that cannot ends the loop (never spins)
            before = len(failed)
            arts = self.board.list(notebook_id)
            status = {a["id"]: a["status"] for a in arts}
            todo = []
            for a in arts:
                if a["status"] != "blocked":
                    continue
                if any(status.get(d) in ("failed", "superseded") or d not in status for d in a["depends_on"]):
                    todo.append((a, "an earlier step it needs failed"))
            if not todo and not any(s in ("ready", "claimed", "drafted", "checking", "approved") for s in status.values()):
                todo = [(a, "nothing was able to run it") for a in arts if a["status"] == "blocked"]
            if not todo:
                return failed
            for a, why in todo:
                if self.board.transition(a["id"], "failed", REFEREE, error=why):
                    failed.append(a["id"])
                    self.emit.emit(notebook_id, REFEREE, "stopped", what=f"{a['type'].replace('_', ' ')}", reason_code="no_progress", reason_text=why,
                                   outcome="failed", severity="warning", chapter_id=a["chapter_id"], artifact_id=a["id"], dedupe_key=f"stuck|{a['id']}")
            if len(failed) == before:                   # nothing new could be failed (a transition refused): stop instead of looping on it
                return failed
        return failed

    # ------------------------------------------------------------------ one pass
    def tick(self, notebook_id: str) -> dict:
        """Recover expired claims, start what became ready, release what may be shown, and fail what can never run."""
        reaped = self.board.reap_expired(notebook_id)
        for r in reaped:
            self.emit.emit(r["notebook_id"], REFEREE, "stopped" if r["to"] == "failed" else "fell_back", what=f"{r['type'].replace('_', ' ')}",
                           reason_code="no_progress" if r["to"] == "failed" else "tool_unavailable", reason_text="it took too long",
                           outcome="failed" if r["to"] == "failed" else "partial", severity="warning" if r["to"] == "failed" else "notice",
                           chapter_id=r["chapter_id"], artifact_id=r["id"], dedupe_key=f"lease|{r['id']}|{r['attempts']}")
        self.board.refresh_ready(notebook_id)
        released = self.release_pending(notebook_id)
        stuck = self.detect_stuck(notebook_id)
        return {"reaped": len(reaped), "released": [a["id"] for a in released], "failed": stuck}
