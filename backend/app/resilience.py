"""Shared resilience pieces, used by BOTH ModelRouter and ToolRunner (nothing here is duplicated elsewhere):

- scrub / mask_key   never let an API key reach a log line, an event row, an error message or a health report
- backoff_delay      exponential backoff with jitter (and Retry-After)
- CircuitBreaker     skip something that keeps failing for a cooldown, then try it again (half-open)
- TokenBucket        client-side rate limiting
- StaleCache         TTL cache that can serve a stale value when the live call fails (stale-while-error)
- EventSink          one place that writes the `events` table and the WARNING log lines
"""
from __future__ import annotations

import logging
import os
import random
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("aasaan.resilience")

# Every environment variable that holds a secret. Their VALUES are scrubbed from any text we emit.
SECRET_ENVS = ("GEMINI_API_KEY", "SARVAM_API_KEY", "TYPESAFE_API_KEY", "OPENROUTER_API_KEY", "YOUTUBE_API_KEY", "JINA_API_KEY",
               "FIRECRAWL_API_KEY", "OPENVERSE_CLIENT_ID", "OPENVERSE_CLIENT_SECRET", "AASAAN_ADMIN_TOKEN")


def scrub(text: Any) -> str:
    """Replace any secret value (and obvious `key=...` / `Bearer ...` shapes) with ***."""
    s = str(text)
    for env in SECRET_ENVS:
        v = os.environ.get(env, "")
        if len(v) >= 6:
            s = s.replace(v, "***")
    s = re.sub(r"(\b(?:key|api_key|apikey|token|access_token|client_secret)=)[^&\s\"']+", r"\1***", s, flags=re.I)
    s = re.sub(r"(Bearer\s+)[A-Za-z0-9._\-]{8,}", r"\1***", s)
    return s


def mask_key(env: str) -> dict:
    """What health endpoints may say about a key: whether it is set. Never a single character of the value."""
    v = os.environ.get(env, "")
    return {"env": env, "set": bool(v), "masked": "********" if v else ""}


# ------------------------------------------------------------------------------ backoff
def backoff_delay(attempt: int, base: float = 0.5, cap: float = 8.0, retry_after: Optional[float] = None) -> float:
    """Exponential backoff with jitter: base * 2**attempt, capped, randomised to 50-100% so retries do not synchronise.
    A server-sent Retry-After wins (but is capped at 30 s)."""
    d = min(cap, base * (2 ** attempt)) * (0.5 + random.random() / 2)
    if retry_after is not None:
        d = max(d, min(30.0, float(retry_after)))
    return d


# ------------------------------------------------------------------------- circuit breaker
class CircuitBreaker:
    """Closed -> (threshold consecutive failures) -> open for cooldown_s -> half-open (one trial) -> closed on success.
    `threshold` and `cooldown_s` are plain attributes so callers can tune them after construction."""

    def __init__(self, threshold: int = 2, cooldown_s: float = 90.0, name: str = ""):
        self.threshold, self.cooldown_s, self.name = threshold, cooldown_s, name
        self.consecutive = 0
        self._open_until = 0.0
        self._lock = threading.Lock()

    def open_remaining(self) -> float:
        with self._lock:
            return max(0.0, self._open_until - time.time())

    def state(self) -> str:
        with self._lock:
            if self._open_until > time.time():
                return "open"
            return "half_open" if self.consecutive >= self.threshold else "closed"

    def record_success(self) -> None:
        with self._lock:
            self.consecutive, self._open_until = 0, 0.0

    def record_failure(self) -> bool:
        """Returns True when this failure tripped the breaker."""
        with self._lock:
            self.consecutive += 1
            if self.consecutive >= self.threshold:
                self._open_until = time.time() + self.cooldown_s
                return True
            return False

    def trip(self, cooldown_s: Optional[float] = None) -> None:
        """Open it right now (for example: startup health check found the model very slow)."""
        with self._lock:
            self.consecutive = max(self.consecutive, self.threshold)
            self._open_until = time.time() + (self.cooldown_s if cooldown_s is None else cooldown_s)


# ----------------------------------------------------------------------------- token bucket
class TokenBucket:
    """`rate` tokens per second up to `burst`. try_acquire never blocks; acquire waits up to `timeout`."""

    def __init__(self, rate: float, burst: int = 1):
        self.rate, self.burst = float(rate), max(1, int(burst))
        self._tokens, self._t = float(self.burst), time.monotonic()
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self.burst, self._tokens + (now - self._t) * self.rate)
            self._t = now
            if self._tokens >= 1:
                self._tokens -= 1
                return True
            return False

    def acquire(self, timeout: float = 5.0) -> bool:
        end = time.monotonic() + timeout
        while True:
            if self.try_acquire():
                return True
            if time.monotonic() >= end:
                return False
            time.sleep(min(0.05, 1.0 / max(self.rate, 0.1)))


# ------------------------------------------------------------------------------ stale cache
class StaleCache:
    """TTL cache on top of the store's key/value table. `get` tells you whether the value is fresh or stale, so a caller
    whose live call failed can still serve the stale value (stale-while-error)."""

    def __init__(self, store, namespace: str):
        self.store, self.ns = store, namespace

    def _k(self, key: str) -> str:
        return f"{self.ns}:{key}"

    def get(self, key: str, ttl_s: float) -> Optional[tuple[Any, float, bool]]:
        """-> (value, age_seconds, is_fresh) or None."""
        if not self.store:
            return None
        hit = self.store.cache_get(self._k(key))
        if not isinstance(hit, dict) or "v" not in hit:
            return None
        age = time.time() - float(hit.get("t", 0))
        return hit["v"], age, age <= ttl_s

    def set(self, key: str, value: Any) -> None:
        if self.store:
            self.store.cache_set(self._k(key), {"v": value, "t": time.time()})


# ------------------------------------------------------------------------------- events
class EventSink:
    """Writes one row per attempt to the `events` table (and a WARNING for anything that is not a plain success).
    Errors and summaries are scrubbed, so a key can never end up in the log or the database."""

    def __init__(self, store=None, enabled: bool = True):
        self.store, self.enabled = store, enabled
        self.hook = None          # optional callable(agent, provider, status, summary, error): the activity feed listens here (never raises into a call)

    def emit(self, agent: str, provider: str, step: int, status: str, latency_ms: int = 0, validation: str = "pass",
             summary: str = "", error: str = "", run_id: Optional[str] = None) -> None:
        err, summ = scrub(error)[:600], scrub(summary)[:300]
        if status in ("retry", "fallback", "failed"):  # never silent: say WHY in the server log
            log.warning("[aasaan] %s step %d (%s) -> %s: %s", agent, step, provider, status, err or summ)
        if self.hook is not None:
            try:
                self.hook(agent, provider, status, summ, err)
            except Exception:  # noqa: BLE001  the feed must never break a model call
                pass
        if self.store and self.enabled:
            self.store.log_event({"run_id": run_id or str(uuid.uuid4()), "ts": datetime.now(timezone.utc).isoformat(), "agent": agent,
                                  "model_used": provider, "fallback_step": step, "status": status, "latency_ms": latency_ms,
                                  "validation": validation, "summary": summ, "error": err})
