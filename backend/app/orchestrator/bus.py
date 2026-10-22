"""Typed agent-to-agent messages, stored durably in `agent_messages`.

Types: ArtifactReady, NeedHelp, Fixes, Clarify, Blocked, Release, Reject, Escalate.
Guards (plain code, no model calls):
  * hop limit: a correlation id carries at most MAX_HOPS messages (the first is hop 0); the next one raises LoopStopped;
  * time to live: an unread message expires and is never delivered;
  * dedupe: an identical open message (same sender, receiver, type, artifact and payload) is not stored twice;
  * who may ask whom: NeedHelp, Clarify and Fixes must follow the sender's `may_request` list in its manifest;
  * Release comes only from the referee.
Receiving is idempotent: a message is handed to exactly one reader and marked handled in the same transaction.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Callable, Optional

from .board import REFEREE, BoardError
from .manifests import Registry

MESSAGE_TYPES = ("ArtifactReady", "NeedHelp", "Fixes", "Clarify", "Blocked", "Release", "Reject", "Escalate")
PEER_TYPES = ("NeedHelp", "Clarify", "Fixes")
MAX_HOPS = 3
DEFAULT_TTL_S = 600


class LoopStopped(BoardError):
    """A reply chain reached the hop limit; the referee records it and stops the exchange."""


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:12]


def _msg(r) -> dict:
    m = dict(r)
    try:
        m["payload"] = json.loads(m["payload"]) if m.get("payload") else None
    except ValueError:
        m["payload"] = None
    return m


class Bus:
    def __init__(self, store, registry: Optional[Registry] = None, clock: Callable[[], float] = time.time):
        self.store = store
        self.registry = registry
        self.now = clock
        self._listeners: list[Callable[[dict], None]] = []

    def subscribe(self, cb: Callable[[dict], None]) -> None:
        """Called after a message is committed (the orchestrator uses this to wake the receiving agent)."""
        self._listeners.append(cb)

    # ------------------------------------------------------------------ send
    def send(self, notebook_id: str, frm: str, to: str, type: str, *, correlation_id: Optional[str] = None, artifact_id: Optional[int] = None,
             payload: Any = None, ttl_s: float = DEFAULT_TTL_S) -> dict:
        return self.send_many([dict(notebook_id=notebook_id, frm=frm, to=to, type=type, correlation_id=correlation_id,
                                    artifact_id=artifact_id, payload=payload, ttl_s=ttl_s)])[0]

    def send_many(self, items: list[dict]) -> list[dict]:
        """Several messages in one short transaction (batched writes). Returns the stored (or already open) messages in order."""
        sent: list[dict] = []
        fresh: list[dict] = []
        t = self.now()
        with self.store.transaction(immediate=True):
            for it in items:
                frm, to, typ = it["frm"], it["to"], it["type"]
                self._check(frm, to, typ)
                corr = it.get("correlation_id") or new_correlation_id()
                payload = json.dumps(it.get("payload"), ensure_ascii=False, sort_keys=True) if it.get("payload") is not None else None
                key = hashlib.sha1(f"{it['notebook_id']}|{frm}|{to}|{typ}|{it.get('artifact_id')}|{payload}".encode()).hexdigest()
                dup = self.store.conn.execute(
                    "SELECT * FROM agent_messages WHERE dedupe_key=? AND handled=0 AND (expires_at IS NULL OR expires_at>?) ORDER BY id LIMIT 1", (key, t)).fetchone()
                if dup:
                    sent.append({**_msg(dup), "deduped": True})
                    continue
                hops = self.store.conn.execute("SELECT COALESCE(MAX(hop_count), -1) AS h FROM agent_messages WHERE correlation_id=?", (corr,)).fetchone()["h"] + 1
                if hops >= MAX_HOPS:
                    raise LoopStopped(f"correlation {corr} reached {MAX_HOPS} messages ({frm} -> {to} {typ})")
                cur = self.store.conn.execute(
                    "INSERT INTO agent_messages(notebook_id,correlation_id,from_agent,to_agent,type,artifact_id,payload,dedupe_key,hop_count,expires_at,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (it["notebook_id"], corr, frm, to, typ, it.get("artifact_id"), payload, key, hops, t + it.get("ttl_s", DEFAULT_TTL_S), t))
                m = _msg(self.store.conn.execute("SELECT * FROM agent_messages WHERE id=?", (cur.lastrowid,)).fetchone())
                sent.append(m)
                fresh.append(m)
        for m in fresh:
            for cb in self._listeners:
                cb(m)
        return sent

    def _check(self, frm: str, to: str, typ: str) -> None:
        if typ not in MESSAGE_TYPES:
            raise BoardError(f"unknown message type {typ!r}")
        if typ == "Release" and frm != REFEREE:
            raise PermissionError("only the referee sends Release")
        if typ in PEER_TYPES and frm != REFEREE and to != REFEREE:
            if self.registry is None:                            # no manifests loaded means no way to check: refuse rather than allow everything
                raise PermissionError(f"cannot check who may send {typ}: the bus has no agent manifests")
            if not self.registry.may_request(frm, to):
                raise PermissionError(f"{frm} may not send {typ} to {to}")

    # ------------------------------------------------------------------ receive
    def receive(self, agent: str, *, limit: int = 10) -> list[dict]:
        """Take up to `limit` unread, unexpired messages for `agent`. Each is delivered once."""
        t = self.now()
        with self.store.transaction(immediate=True):
            rows = self.store.conn.execute(
                "SELECT * FROM agent_messages WHERE to_agent=? AND handled=0 AND (expires_at IS NULL OR expires_at>?) ORDER BY id LIMIT ?", (agent, t, limit)).fetchall()
            mine = []
            for r in rows:                                       # belt and braces: a row is delivered only if THIS call flipped it from unread to handled
                if self.store.conn.execute("UPDATE agent_messages SET handled=1 WHERE id=? AND handled=0", (r["id"],)).rowcount == 1:
                    mine.append(r)
            return [_msg(r) for r in mine]

    def pending(self, agent: Optional[str] = None) -> int:
        sql, args = "SELECT COUNT(*) AS n FROM agent_messages WHERE handled=0 AND (expires_at IS NULL OR expires_at>?)", [self.now()]
        if agent:
            sql, args = sql + " AND to_agent=?", args + [agent]
        return self.store._one(sql, tuple(args))["n"]

    def sweep(self) -> int:
        """Expire unread messages whose time to live ran out (marked handled so they are never delivered). Returns how many."""
        with self.store.transaction(immediate=True):
            return self.store.conn.execute("UPDATE agent_messages SET handled=1 WHERE handled=0 AND expires_at IS NOT NULL AND expires_at<=?", (self.now(),)).rowcount

    def thread(self, correlation_id: str) -> list[dict]:
        return [_msg(r) for r in self.store._all("SELECT * FROM agent_messages WHERE correlation_id=? ORDER BY id", (correlation_id,))]
