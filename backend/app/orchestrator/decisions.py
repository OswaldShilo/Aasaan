"""DecisionEmitter: the one place every meaningful agent decision is recorded (table `decision_log`).

The referee, the agents, the router and the tool runner all call `emit`; nothing else writes to the table. The learner-facing sentence
(`summary`) is assembled HERE from fixed templates and a few short fields. Free model prose never goes in, and raw prompts, keys and file
paths are scrubbed. Technical detail (model used, fallback step, latency) stays in the `events` table, linked by `event_id`.
This module does not depend on how orchestration is done, so a later framework can feed it the same way.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Optional

KINDS = ("planned", "accepted", "rejected", "fixed", "chose", "expanded", "fell_back", "released", "stopped", "asked", "answered", "proposed", "marked")
OUTCOMES = ("ok", "partial", "failed", "skipped")
SEVERITIES = ("info", "notice", "warning")
# the fixed reason vocabulary: code -> plain words
REASONS = {
    "off_topic": "off topic", "wrong_language": "wrong language", "not_embeddable": "it cannot be embedded", "unsupported_claim": "not supported by the source",
    "too_short": "too short", "duplicate_of_notes": "it repeats the notes", "ambiguous_question": "it had two possible answers", "rate_limited": "a rate limit",
    "depends_on": "it depends on an earlier step", "user_opened": "you opened it", "loop_limit": "it reached its limit", "no_progress": "nothing could run",
    "tool_unavailable": "a tool was unavailable", "backup_model": "a backup model was used", "other": "",
}
AGENT_NAMES = {
    "intake": "Intake", "scoper": "Scoper", "architect": "Architect", "curator": "Curator", "teacher": "Teacher", "critic": "Critic", "quizzer": "Quizzer",
    "illustrator": "Illustrator", "slides": "Slides", "diagnoser": "Diagnoser", "rewriter": "Rewriter", "editor": "Editor", "companion": "Companion",
    "doubt": "Doubt answerer", "practice": "Practice Curator", "referee": "Aasaan",
}
TEMPLATES = {
    "planned": "{agent} planned {what}.", "accepted": "{agent} accepted {what}.", "rejected": "{agent} rejected {what}{because}.",
    "fixed": "{agent} fixed {what}{because}.", "chose": "{agent} chose {what}{because}.", "expanded": "{agent} added more to {what}{because}.",
    "fell_back": "{agent} used a backup for {what}{because}.", "released": "{what} is ready.", "stopped": "{agent} stopped {what}{because}.",
    "asked": "{agent} asked for help with {what}.", "answered": "{agent} answered {what}.", "proposed": "{agent} proposed {what}. Waiting for your approval.",
    "marked": "{agent} marked {what}.",
}
_SCRUB = [
    (re.compile(r"\b(?:sk|AIza|gsk|xai|ghp|gho|hf|tsk)[-_A-Za-z0-9]{8,}"), "[removed]"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]{8,}"), "[removed]"),
    (re.compile(r"(?i)\b[a-z]:\\[^\s]*"), "[removed]"),
    (re.compile(r"(?<![\w.])/(?:home|users|tmp|var|etc|usr|app|data)/[^\s]*", re.I), "[removed]"),
    (re.compile(r"(?i)\b(?:system|user)\s*prompt\s*[:=].*"), "[removed]"),      # a prompt being pasted in, not the words "user prompt"
]
MAX_WORDS = 12


def clean(text: Any, words: int = MAX_WORDS, chars: int = 90) -> str:
    """Short plain text: scrubbed of keys, paths and prompt markers, newlines folded, at most `words` words."""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    for rx, rep in _SCRUB:
        s = rx.sub(rep, s)
    parts = s.split(" ")
    if len(parts) > words:
        s = " ".join(parts[:words])
    return s[:chars].rstrip(" ,;:")


def _counts_text(counts: Optional[dict]) -> str:
    if not counts:
        return ""
    bits = [f"{k} {int(v)}" for k, v in counts.items() if isinstance(v, (int, float))]
    return f" ({', '.join(bits)})" if bits else ""


class DecisionEmitter:
    def __init__(self, store, clock: Callable[[], float] = time.time):
        self.store = store
        self.now = clock
        self._listeners: list[Callable[[dict], None]] = []

    def subscribe(self, cb: Callable[[dict], None]) -> None:
        """Called after each new record is committed (the activity feed will use this; batching lives there)."""
        self._listeners.append(cb)

    def emit(self, notebook_id: str, agent: str, kind: str, *, what: str = "", reason_code: Optional[str] = None, reason_text: str = "",
             outcome: str = "ok", counts: Optional[dict] = None, evidence: Optional[list[dict]] = None, severity: str = "info",
             chapter_id: Optional[int] = None, artifact_id: Optional[int] = None, correlation_id: Optional[str] = None,
             event_id: Optional[int] = None, dedupe_key: Optional[str] = None) -> Optional[dict]:
        """Record one decision. Returns the record, or None when the same decision was already recorded (dedupe)."""
        if kind not in KINDS:
            raise ValueError(f"unknown decision kind {kind!r}")
        if outcome not in OUTCOMES or severity not in SEVERITIES:
            raise ValueError(f"bad outcome {outcome!r} or severity {severity!r}")
        code = reason_code if reason_code in REASONS else ("other" if reason_code else None)
        why = clean(reason_text) or REASONS.get(code or "", "")
        what_s = clean(what, words=14, chars=120) or "this step"
        summary = TEMPLATES[kind].format(agent=AGENT_NAMES.get(agent, clean(agent, 3)), what=what_s, because=f": {why}" if why else "")
        if counts:
            summary = summary.rstrip(".") + _counts_text(counts) + "."
        summary = summary[0].upper() + summary[1:]
        key = dedupe_key or (f"{correlation_id}|{agent}|{kind}|{artifact_id}|{code}" if correlation_id else None)
        ev = [{"type": clean(e.get("type"), 2, 20), "ref": clean(e.get("ref"), 3, 40), "label": clean(e.get("label"), 8, 60)} for e in (evidence or [])][:12]
        t = self.now()
        with self.store.transaction(immediate=True):
            cur = self.store.conn.execute(
                "INSERT OR IGNORE INTO decision_log(notebook_id,chapter_id,artifact_id,correlation_id,event_id,ts,agent,kind,summary,reason_code,reason_text,outcome,counts,evidence,severity,dedupe_key)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (notebook_id, chapter_id, artifact_id, correlation_id, event_id, t, agent, kind, summary, code, why, outcome,
                 json.dumps(counts or {}), json.dumps(ev, ensure_ascii=False), severity, key))
            if cur.rowcount != 1:
                return None
            rec = self._row(self.store.conn.execute("SELECT * FROM decision_log WHERE id=?", (cur.lastrowid,)).fetchone())
        for cb in self._listeners:
            cb(rec)
        return rec

    def last_id(self, notebook_id: str) -> int:
        r = self.store._one("SELECT COALESCE(MAX(id), 0) AS n FROM decision_log WHERE notebook_id=?", (notebook_id,))
        return int(r["n"])

    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        d["counts"] = json.loads(d["counts"] or "{}")
        d["evidence"] = json.loads(d["evidence"] or "[]")
        d.pop("dedupe_key", None)
        return d

    def recent(self, notebook_id: str, *, since_id: int = 0, chapter_id: Optional[int] = None, agent: Optional[str] = None,
               severity: Optional[str] = None, limit: int = 200) -> list[dict]:
        sql, args = "SELECT * FROM decision_log WHERE notebook_id=? AND id>?", [notebook_id, since_id]
        for col, val in (("chapter_id", chapter_id), ("agent", agent), ("severity", severity)):
            if val is not None:
                sql += f" AND {col}=?"
                args.append(val)
        return [self._row(r) for r in self.store._all(sql + " ORDER BY id LIMIT ?", (*args, limit))]
