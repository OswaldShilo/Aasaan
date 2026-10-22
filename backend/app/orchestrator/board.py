"""The shared board: typed artifacts with a status lifecycle, atomic claims and leases.

    blocked -> ready -> claimed -> drafted -> checking -> approved -> released -> superseded      (any live state -> failed)

Rules, all enforced here in code (no model calls, no router import):
  * every state change runs in a short BEGIN IMMEDIATE transaction and re-reads the row inside it, so two workers can never both win;
  * only the referee may move an artifact to `released` (agents get PermissionError);
  * a claim is atomic: it sets claimed_by and lease_until only if the task is still `ready` and unclaimed;
  * a lease that expires puts the task back to `ready` (or `failed` once its retries are used up), so a crashed agent never strands work;
  * transitions are idempotent: asking for the status an artifact already has is a no-op, not an error.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Iterable, Optional

from .manifests import Registry

REFEREE = "referee"
STATUSES = ("blocked", "ready", "claimed", "drafted", "checking", "approved", "released", "failed", "superseded")
DONE_STATES = ("approved", "released")            # a dependency is satisfied once it is approved or released
LIVE_STATES = ("blocked", "ready", "claimed", "drafted", "checking", "approved")
TRANSITIONS = {
    "blocked": {"ready", "failed", "superseded"},
    "ready": {"claimed", "failed", "superseded"},
    "claimed": {"drafted", "failed", "ready", "superseded"},
    "drafted": {"checking", "approved", "released", "failed", "ready", "superseded"},
    "checking": {"approved", "drafted", "failed", "superseded"},
    "approved": {"released", "superseded", "failed"},
    "released": {"superseded"},
    "failed": {"ready", "superseded"},
    "superseded": set(),
}
# what an agent (not the referee) may do on its own claimed or checked artifact
AGENT_MOVES = {("claimed", "drafted"), ("claimed", "failed"), ("claimed", "ready"), ("drafted", "checking"), ("drafted", "ready"),
               ("checking", "approved"), ("checking", "drafted"), ("checking", "failed")}
DEFAULT_LEASE_S = 120
MAX_REFUNDS = 2


class BoardError(RuntimeError):
    pass


class StaleTransition(BoardError):
    """The artifact is no longer in the state the caller expected (someone else moved it)."""


def _j(v: Any) -> Optional[str]:
    return None if v is None else json.dumps(v, ensure_ascii=False)


def _l(s: Optional[str], default: Any = None) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default


def _flags(raw: Optional[str]) -> dict:
    v = _l(raw, {})
    return v if isinstance(v, dict) else {}


def _art(r) -> dict:
    a = dict(r)
    a["payload"] = _l(a.get("payload"))
    a["depends_on"] = _l(a.get("depends_on"), [])
    a["flags"] = _flags(a.get("flags"))
    return a


class Board:
    def __init__(self, store, registry: Optional[Registry] = None, clock: Callable[[], float] = time.time):
        self.store = store
        self.registry = registry
        self.now = clock

    # ------------------------------------------------------------------ create and read
    def add(self, notebook_id: str, type: str, *, chapter_id: Optional[int] = None, owner_agent: Optional[str] = None,
            payload: Any = None, depends_on: Iterable[int] = (), priority: int = 50, flags: Optional[dict] = None) -> int:
        deps = list(depends_on)
        t = self.now()
        with self.store.transaction(immediate=True):
            if deps:
                marks = ",".join("?" * len(set(deps)))
                have = {r["id"] for r in self.store.conn.execute(f"SELECT id FROM artifacts WHERE id IN ({marks})", tuple(set(deps))).fetchall()}
                if have != set(deps):
                    raise BoardError(f"depends_on names artifact(s) that do not exist: {sorted(set(deps) - have)}")
            status = "ready" if self._deps_met(deps) else "blocked"
            cur = self.store.conn.execute(
                "INSERT INTO artifacts(notebook_id,chapter_id,type,version,status,owner_agent,payload,depends_on,flags,priority,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (notebook_id, chapter_id, type, 1, status, owner_agent, _j(payload), _j(deps), _j(flags or {}), priority, t, t))
            return int(cur.lastrowid)

    def get(self, aid: int) -> Optional[dict]:
        r = self.store._one("SELECT * FROM artifacts WHERE id=?", (aid,))
        return _art(r) if r else None

    def list(self, notebook_id: str, *, chapter_id: Optional[int] = None, type: Optional[str] = None, status: Optional[str] = None) -> list[dict]:
        sql, args = "SELECT * FROM artifacts WHERE notebook_id=?", [notebook_id]
        for col, val in (("chapter_id", chapter_id), ("type", type), ("status", status)):
            if val is not None:
                sql += f" AND {col}=?"
                args.append(val)
        return [_art(r) for r in self.store._all(sql + " ORDER BY priority, id", tuple(args))]

    def _deps_met(self, deps: list[int]) -> bool:
        if not deps:
            return True
        marks = ",".join("?" * len(deps))
        n = self.store.conn.execute(f"SELECT COUNT(*) AS n FROM artifacts WHERE id IN ({marks}) AND status IN ('approved','released')", tuple(deps)).fetchone()["n"]
        return n == len(set(deps))

    # ------------------------------------------------------------------ readiness
    def refresh_ready(self, notebook_id: Optional[str] = None) -> list[int]:
        """Blocked tasks whose dependencies are all approved or released become ready. Returns the ids that moved."""
        moved: list[int] = []
        with self.store.transaction(immediate=True):
            sql, args = "SELECT id, depends_on FROM artifacts WHERE status='blocked'", ()
            if notebook_id:
                sql, args = sql + " AND notebook_id=?", (notebook_id,)
            for r in self.store.conn.execute(sql, args).fetchall():
                if self._deps_met(_l(r["depends_on"], [])):
                    self.store.conn.execute("UPDATE artifacts SET status='ready', updated_at=? WHERE id=? AND status='blocked'", (self.now(), r["id"]))
                    moved.append(r["id"])
        return moved

    # ------------------------------------------------------------------ claims
    def claim(self, agent: str, *, lease_s: float = DEFAULT_LEASE_S, notebook_id: Optional[str] = None) -> Optional[dict]:
        """Atomically claim the highest-priority ready task for `agent`, or None. Honours the agent's manifest concurrency."""
        produces: tuple[str, ...] = ()
        cap = 1
        if self.registry:
            m = self.registry.get(agent)
            produces, cap = m.produces, m.concurrency
        t = self.now()
        with self.store.transaction(immediate=True):
            running = self.store.conn.execute(
                "SELECT COUNT(*) AS n FROM artifacts WHERE status='claimed' AND claimed_by=? AND lease_until>?", (agent, t)).fetchone()["n"]
            if running >= cap:
                return None
            sql = "SELECT id FROM artifacts WHERE status='ready' AND claimed_by IS NULL AND (owner_agent=?"
            args: list[Any] = [agent]
            if produces:
                sql += f" OR (owner_agent IS NULL AND type IN ({','.join('?' * len(produces))}))"
                args += list(produces)
            sql += ")"
            if notebook_id:
                sql, args = sql + " AND notebook_id=?", args + [notebook_id]
            row = self.store.conn.execute(sql + " ORDER BY priority, id LIMIT 1", tuple(args)).fetchone()
            if not row:
                return None
            cur = self.store.conn.execute(
                "UPDATE artifacts SET status='claimed', claimed_by=?, lease_until=?, attempts=attempts+1, updated_at=? WHERE id=? AND status='ready' AND claimed_by IS NULL",
                (agent, t + lease_s, t, row["id"]))
            if cur.rowcount != 1:
                return None
            aid = row["id"]
        return self.get(aid)

    def heartbeat(self, aid: int, agent: str, lease_s: float = DEFAULT_LEASE_S) -> bool:
        """Extend a lease while the agent is still working. False if the claim was lost."""
        with self.store.transaction(immediate=True):
            cur = self.store.conn.execute("UPDATE artifacts SET lease_until=?, updated_at=? WHERE id=? AND status='claimed' AND claimed_by=?",
                                          (self.now() + lease_s, self.now(), aid, agent))
            return cur.rowcount == 1

    def reap_expired(self, notebook_id: Optional[str] = None) -> list[dict]:
        """Claims whose lease ran out go back to `ready`, or to `failed` once the manifest's retries are spent. Returns what moved.
        `max_retries` counts retries AFTER the first attempt: with max_retries=1 a task gets two attempts (attempts=1 -> ready, attempts=2 -> failed).
        An expired claim is not reclaimed until this runs (the referee tick does it), so it still counts against nobody's concurrency cap."""
        out: list[dict] = []
        t = self.now()
        with self.store.transaction(immediate=True):
            sql, args = "SELECT * FROM artifacts WHERE status='claimed' AND lease_until<=?", [t]
            if notebook_id:
                sql, args = sql + " AND notebook_id=?", args + [notebook_id]
            for r in self.store.conn.execute(sql, tuple(args)).fetchall():
                retries = self.registry.get(r["claimed_by"]).max_retries if self.registry and r["claimed_by"] in self.registry.agents else 1
                spent = r["attempts"] > retries
                to = "failed" if spent else "ready"
                self.store.conn.execute(
                    "UPDATE artifacts SET status=?, claimed_by=NULL, lease_until=NULL, error=?, updated_at=? WHERE id=? AND status='claimed'",
                    (to, "lease expired; retries used up" if spent else r["error"], t, r["id"]))
                out.append({"id": r["id"], "agent": r["claimed_by"], "to": to, "notebook_id": r["notebook_id"], "chapter_id": r["chapter_id"], "type": r["type"], "attempts": r["attempts"]})
        return out

    def recover_after_restart(self) -> int:
        """On start, nothing is really running: every claim returns to `ready`. The interrupted attempt is refunded (a restart is not the task's fault),
        but at most MAX_REFUNDS times per task, so a task that itself crashes the worker still runs out of retries instead of looping forever."""
        with self.store.transaction(immediate=True):
            rows = self.store.conn.execute("SELECT id, attempts, flags FROM artifacts WHERE status='claimed'").fetchall()
            for r in rows:
                flags = _flags(r["flags"])
                refunds = int(flags.get("restart_refunds", 0))
                refund = 1 if refunds < MAX_REFUNDS else 0
                flags["restart_refunds"] = refunds + 1
                self.store.conn.execute("UPDATE artifacts SET status='ready', claimed_by=NULL, lease_until=NULL, attempts=MAX(attempts-?, 0), flags=?, updated_at=? WHERE id=?",
                                        (refund, _j(flags), self.now(), r["id"]))
            return len(rows)

    # ------------------------------------------------------------------ transitions
    def transition(self, aid: int, to: str, actor: str, *, payload: Any = None, error: Optional[str] = None, expect: Optional[str] = None) -> bool:
        """Move an artifact. False when it already has that status (idempotent). Raises on illegal moves."""
        if to not in STATUSES:
            raise BoardError(f"unknown status {to!r}")
        if to == "released" and actor != REFEREE:
            raise PermissionError("only the referee releases artifacts")
        with self.store.transaction(immediate=True):
            r = self.store.conn.execute("SELECT * FROM artifacts WHERE id=?", (aid,)).fetchone()
            if r is None:
                raise BoardError(f"artifact {aid} does not exist")
            cur = r["status"]
            if cur == to:
                return False
            if expect and cur != expect:
                raise StaleTransition(f"artifact {aid} is {cur}, expected {expect}")
            if to not in TRANSITIONS[cur]:
                raise BoardError(f"artifact {aid}: {cur} -> {to} is not allowed")
            if actor != REFEREE:
                if (cur, to) not in AGENT_MOVES:
                    raise PermissionError(f"{actor} may not move an artifact {cur} -> {to}")
                if cur == "claimed" and r["claimed_by"] != actor:
                    raise PermissionError(f"artifact {aid} is claimed by {r['claimed_by']}, not {actor}")
            sets, args = ["status=?", "updated_at=?"], [to, self.now()]
            if payload is not None:
                sets.append("payload=?")
                args.append(_j(payload))
            if error is not None:
                sets.append("error=?")
                args.append(error)
            if to != "claimed":                  # a claim only lives while the task is claimed
                sets += ["claimed_by=NULL", "lease_until=NULL"]
            self.store.conn.execute(f"UPDATE artifacts SET {', '.join(sets)} WHERE id=?", (*args, aid))
        return True

    def revise(self, aid: int, actor: str, payload: Any) -> int:
        """A fix landed (Critic -> Teacher): replace the payload in place, bump the version, and put the artifact back to `drafted`."""
        with self.store.transaction(immediate=True):
            r = self.store.conn.execute("SELECT status, version, claimed_by FROM artifacts WHERE id=?", (aid,)).fetchone()
            if r is None or r["status"] not in ("checking", "drafted", "claimed"):
                raise StaleTransition(f"artifact {aid} cannot be revised in state {r['status'] if r else 'missing'}")
            if r["status"] == "claimed" and actor != REFEREE and r["claimed_by"] != actor:
                raise PermissionError(f"artifact {aid} is claimed by {r['claimed_by']}, not {actor}")
            self.store.conn.execute("UPDATE artifacts SET payload=?, version=version+1, status='drafted', claimed_by=NULL, lease_until=NULL, updated_at=? WHERE id=?",
                                    (_j(payload), self.now(), aid))
            return int(r["version"]) + 1

    def patch_released(self, aid: int, payload: Any, flags: Optional[dict] = None, actor: str = "") -> int:
        """Referee only (callers go through Referee.upgrade_released): patch a released artifact in place, e.g. Critic fixes, and bump the version."""
        if actor != REFEREE:
            raise PermissionError("only the referee patches a released artifact")
        with self.store.transaction(immediate=True):
            r = self.store.conn.execute("SELECT status, version, flags FROM artifacts WHERE id=?", (aid,)).fetchone()
            if r is None or r["status"] != "released":
                raise StaleTransition(f"artifact {aid} is not released")
            merged = {**_flags(r["flags"]), **(flags or {})}
            self.store.conn.execute("UPDATE artifacts SET payload=?, flags=?, version=version+1, updated_at=? WHERE id=?", (_j(payload), _j(merged), self.now(), aid))
            return int(r["version"]) + 1

    def set_flag(self, aid: int, **flags: Any) -> dict:
        with self.store.transaction(immediate=True):
            r = self.store.conn.execute("SELECT flags FROM artifacts WHERE id=?", (aid,)).fetchone()
            if r is None:
                raise BoardError(f"artifact {aid} does not exist")
            merged = {**_flags(r["flags"]), **flags}
            self.store.conn.execute("UPDATE artifacts SET flags=?, updated_at=? WHERE id=?", (_j(merged), self.now(), aid))
            return merged
