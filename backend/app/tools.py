"""ToolRunner: every web or data tool (Wikipedia, Commons, Openverse, Jina, Firecrawl, YouTube, ...) goes through here.

One place gives all of them: a structured result, classified errors, retries with backoff and jitter, a per-tool circuit
breaker and token bucket, per-notebook budgets, a TTL cache that can serve stale data when the live call fails, an `events`
row for every attempt (with the reason, scrubbed of secrets), and the AASAAN_CHAOS switch used to test failure paths.

It shares its building blocks with ModelRouter (app/resilience.py); nothing is duplicated.

ToolResult = {ok, data, error:{kind, message, retryable}, provider, latency_ms, cached, stale}
Error kinds: rate_limit (429), server (5xx), timeout, auth (401/403/402), invalid (bad or empty output), thin (too little
content), budget, disabled, unconfigured, circuit_open, chaos.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx

from .resilience import CircuitBreaker, EventSink, StaleCache, TokenBucket, backoff_delay, mask_key, scrub

log = logging.getLogger("aasaan.tools")


# ------------------------------------------------------------------------------ results
@dataclass
class ToolError:
    kind: str
    message: str
    retryable: bool = False

    def to_dict(self) -> dict:
        return {"kind": self.kind, "message": self.message, "retryable": self.retryable}


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: Optional[ToolError] = None
    provider: str = ""
    latency_ms: int = 0
    cached: bool = False
    stale: bool = False

    def value_or(self, default: Any = None) -> Any:
        return self.data if self.ok else default

    def to_dict(self) -> dict:
        return {"ok": self.ok, "data": self.data, "error": self.error.to_dict() if self.error else None, "provider": self.provider,
                "latency_ms": self.latency_ms, "cached": self.cached, "stale": self.stale}


class ToolFailure(Exception):
    """Raised by a tool function to report a classified failure."""

    def __init__(self, kind: str, message: str, retryable: bool = False, status: Optional[int] = None, retry_after: Optional[float] = None):
        super().__init__(message)
        self.kind, self.message, self.retryable, self.status, self.retry_after = kind, message, retryable, status, retry_after


def check_response(resp, what: str = "") -> None:
    """Turn an HTTP response into a classified ToolFailure (or return if it is a success)."""
    code = int(getattr(resp, "status_code", 200))
    if code < 400:
        return
    body = " ".join(str(getattr(resp, "text", ""))[:200].split())
    ra = None
    try:
        ra = float(resp.headers.get("retry-after")) if hasattr(resp, "headers") and resp.headers.get("retry-after") else None
    except (TypeError, ValueError):
        ra = None
    msg = f"{what + ' ' if what else ''}HTTP {code}: {body}"
    if code in (401, 402, 403):
        raise ToolFailure("auth", msg, False, status=code)
    if code == 429:
        raise ToolFailure("rate_limit", msg, True, status=code, retry_after=ra)
    if code == 408:
        raise ToolFailure("timeout", msg, True, status=code)
    if code >= 500:
        raise ToolFailure("server", msg, True, status=code)
    raise ToolFailure("invalid", msg, False, status=code)


# ------------------------------------------------------------------------------- chaos
class Chaos:
    """AASAAN_CHAOS="tool:commons=fail,tool:firecrawl=timeout,model:jev=fail"  (modes: fail, timeout, rate_limit, empty).
    Lets you prove every fallback path without breaking real services. Also settable at runtime via the admin endpoint."""

    MODES = {"fail", "timeout", "rate_limit", "empty"}

    def __init__(self, spec: str = ""):
        self.rules: dict[tuple[str, str], str] = {}
        self.set(spec)

    @classmethod
    def from_env(cls) -> "Chaos":
        return cls(os.environ.get("AASAAN_CHAOS", ""))

    def set(self, spec: str) -> None:
        rules: dict[tuple[str, str], str] = {}
        for part in (spec or "").split(","):
            part = part.strip()
            if not part or "=" not in part or ":" not in part.split("=", 1)[0]:
                continue
            target, mode = part.split("=", 1)
            kind, name = target.split(":", 1)
            if kind in ("tool", "model") and mode.strip() in self.MODES:
                rules[(kind, name.strip())] = mode.strip()
        self.rules = rules

    def mode_for(self, kind: str, name: str) -> Optional[str]:
        return self.rules.get((kind, name))

    def describe(self) -> str:
        return ",".join(f"{k}:{n}={m}" for (k, n), m in sorted(self.rules.items()))


# Failure kinds that mean "the tool itself is unhealthy" (they feed the breaker and the degraded state).
OUTAGE_KINDS = {"server", "timeout", "rate_limit", "auth", "chaos"}


# ------------------------------------------------------------------------------- specs
@dataclass
class ToolSpec:
    name: str
    description: str = ""
    key_env: Optional[str] = None            # an API key this tool uses
    key_required: bool = False               # False = works anonymously (higher limits with a key)
    rate_per_s: float = 5.0
    burst: int = 5
    retries: int = 1                         # retries on a retryable error (so 2 attempts by default)
    breaker_threshold: int = 3
    breaker_cooldown_s: float = 60.0
    budget_env: Optional[str] = None         # env var with the per-notebook call cap
    budget_default: Optional[int] = None
    cache_ttl_s: float = 7 * 86400
    disable_on_status: tuple = ()            # HTTP statuses that switch the tool off for the rest of the session
    integrated: bool = True
    extra_keys: tuple = ()                   # more env vars shown (masked) in health, e.g. Openverse client id/secret


DEFAULT_SPECS: dict[str, ToolSpec] = {s.name: s for s in [
    ToolSpec("jina", "Jina Reader (tier 1)", key_env="JINA_API_KEY", rate_per_s=2, burst=2, budget_env="JINA_MAX_CALLS_PER_NOTEBOOK",
             budget_default=40, cache_ttl_s=24 * 3600),
    ToolSpec("firecrawl", "Firecrawl scrape (last page tier) and search (discovery fallback)", key_env="FIRECRAWL_API_KEY", key_required=True,
             rate_per_s=2, burst=2, retries=1, budget_env="FIRECRAWL_MAX_CALLS_PER_NOTEBOOK", budget_default=20,
             disable_on_status=(401, 402), cache_ttl_s=30 * 86400),
    ToolSpec("wikipedia", "Wikipedia search and page summaries", rate_per_s=5, burst=5, cache_ttl_s=7 * 86400),
    ToolSpec("codeforces", "Codeforces public API: the problem list for practice (no key; statements are never fetched)", rate_per_s=0.5, burst=2, retries=1,
             budget_env="CODEFORCES_MAX_CALLS_PER_NOTEBOOK", budget_default=10, cache_ttl_s=86400),
    ToolSpec("openlibrary", "Open Library book search (free, no key): the books Aasaan sources for a topic", rate_per_s=2, burst=3, retries=1,
             budget_env="OPENLIBRARY_MAX_CALLS_PER_NOTEBOOK", budget_default=6, cache_ttl_s=7 * 86400),
    ToolSpec("commons", "Wikimedia Commons image search and attribution", rate_per_s=2, burst=3, budget_env="COMMONS_MAX_CALLS_PER_NOTEBOOK",
             budget_default=12, cache_ttl_s=7 * 86400),
    ToolSpec("openverse", "Openverse image search (anonymous is very tight: 20/min, 200/day)", key_env="OPENVERSE_CLIENT_ID",
             extra_keys=("OPENVERSE_CLIENT_SECRET",), rate_per_s=0.3, burst=3, retries=0, budget_env="OPENVERSE_MAX_CALLS_PER_NOTEBOOK",
             budget_default=6, cache_ttl_s=14 * 86400),
    ToolSpec("youtube", "YouTube Data API search", key_env="YOUTUBE_API_KEY", key_required=True, rate_per_s=5, burst=5,
             budget_env="YOUTUBE_MAX_CALLS_PER_NOTEBOOK", budget_default=40, cache_ttl_s=3 * 86400),
    ToolSpec("oembed", "YouTube oEmbed (does this video exist and embed?)", rate_per_s=5, burst=5, cache_ttl_s=3 * 86400),
    ToolSpec("codeforces", "Codeforces problems (not integrated in this codebase yet)", integrated=False),
]}


# ------------------------------------------------------------------------------ budgets
current_notebook: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("aasaan_notebook", default=None)


class Budgets:
    """Per-notebook call counters. Outside a notebook scope nothing is capped."""

    def __init__(self):
        self.used: dict[str, dict[str, int]] = {}
        self._lock = threading.Lock()

    def limit(self, spec: ToolSpec) -> Optional[int]:
        if spec.budget_env and os.environ.get(spec.budget_env, "").strip().isdigit():
            return int(os.environ[spec.budget_env])
        return spec.budget_default

    def allow(self, spec: ToolSpec) -> bool:
        nb = current_notebook.get()
        lim = self.limit(spec)
        if nb is None or lim is None:
            return True
        with self._lock:
            return self.used.get(nb, {}).get(spec.name, 0) < lim

    def charge(self, spec: ToolSpec) -> None:
        nb = current_notebook.get()
        if nb is None:
            return
        with self._lock:
            d = self.used.setdefault(nb, {})
            d[spec.name] = d.get(spec.name, 0) + 1

    def usage(self, nb: str, tool: str) -> int:
        return self.used.get(nb, {}).get(tool, 0)


# ------------------------------------------------------------------------------- runner
@dataclass
class _Stats:
    calls: int = 0
    errors: int = 0
    cache_hits: int = 0
    last_error: str = ""
    last_error_kind: str = ""
    last_error_ts: float = 0.0
    last_success_ts: float = 0.0
    last_ok: Optional[bool] = None


class ToolRunner:
    def __init__(self, store=None, sink: Optional[EventSink] = None, chaos: Optional[Chaos] = None,
                 specs: Optional[dict[str, ToolSpec]] = None, rate_scale: float = 1.0):
        self.rate_scale = rate_scale  # tests use a large value so rate limiting does not slow them down
        self.store = store
        self.sink = sink or EventSink(store)
        self.chaos = chaos or Chaos.from_env()
        self.specs = dict(specs or DEFAULT_SPECS)
        self.cache = StaleCache(store, "tool")
        self.budgets = Budgets()
        self._breakers: dict[str, CircuitBreaker] = {}
        self._buckets: dict[str, TokenBucket] = {}
        self._stats: dict[str, _Stats] = {}
        self._disabled: dict[str, str] = {}
        self._local: dict[str, Callable[[], dict]] = {}
        self._lock = threading.Lock()
        self.sleep: Callable[[float], None] = time.sleep   # tests replace this

    # ---- helpers
    def spec(self, tool: str) -> ToolSpec:
        if tool not in self.specs:
            self.specs[tool] = ToolSpec(tool)
        return self.specs[tool]

    def _breaker(self, tool: str) -> CircuitBreaker:
        with self._lock:
            br = self._breakers.get(tool)
            if br is None:
                sp = self.spec(tool)
                br = self._breakers[tool] = CircuitBreaker(sp.breaker_threshold, sp.breaker_cooldown_s, tool)
            return br

    def _bucket(self, tool: str) -> TokenBucket:
        with self._lock:
            b = self._buckets.get(tool)
            if b is None:
                sp = self.spec(tool)
                b = self._buckets[tool] = TokenBucket(sp.rate_per_s * self.rate_scale, sp.burst)
            return b

    def _st(self, tool: str) -> _Stats:
        with self._lock:
            return self._stats.setdefault(tool, _Stats())

    @contextlib.contextmanager
    def notebook_scope(self, notebook_id: str):
        """Calls made inside count against this notebook's per-tool budgets."""
        tok = current_notebook.set(notebook_id)
        try:
            yield
        finally:
            current_notebook.reset(tok)

    def disable(self, tool: str, reason: str) -> None:
        self._disabled[tool] = reason
        log.warning("[aasaan] tool %s DISABLED for this session: %s", tool, reason)

    def enable(self, tool: Optional[str] = None) -> None:
        if tool:
            self._disabled.pop(tool, None)
        else:
            self._disabled.clear()

    def register_local(self, name: str, check: Callable[[], dict]) -> None:
        """Deterministic in-process tools (renderers, parsers) appear in /api/health/tools through a self-check."""
        self._local[name] = check

    # ---- the call
    def run(self, tool: str, fn: Callable[[], Any], *, cache_key: Optional[str] = None, validate: Optional[Callable[[Any], None]] = None,
            min_chars: Optional[int] = None, summary: str = "") -> ToolResult:
        sp, st = self.spec(tool), self._st(tool)
        run_id, label, t0 = str(uuid.uuid4()), summary or tool, time.perf_counter()
        ms = lambda: int((time.perf_counter() - t0) * 1000)  # noqa: E731

        def finish_fail(err: ToolError, status: str = "fallback") -> ToolResult:
            st.errors += 1
            st.last_error, st.last_error_kind, st.last_error_ts = scrub(err.message)[:300], err.kind, time.time()
            if err.kind in OUTAGE_KINDS:
                st.last_ok = False
            self.sink.emit(f"tool:{tool}", tool, 0, status, ms(), "fail", label, f"[{err.kind}] {err.message}", run_id)
            return ToolResult(False, None, ToolError(err.kind, scrub(err.message)[:300], err.retryable), tool, ms())

        # 1. switched off, not configured, chaos-killed before we even try
        if tool in self._disabled:
            return finish_fail(ToolError("disabled", f"{tool} is disabled for this session ({self._disabled[tool]})"))
        if sp.key_required and sp.key_env and not os.environ.get(sp.key_env):
            return finish_fail(ToolError("unconfigured", f"{sp.key_env} is not set"))
        mode = self.chaos.mode_for("tool", tool)

        # 2. fresh cache
        cached = self.cache.get(cache_key, sp.cache_ttl_s) if cache_key and sp.cache_ttl_s else None
        if cached and cached[2] and mode is None:
            st.cache_hits += 1
            self.sink.emit(f"tool:{tool}", tool, 0, "ok", ms(), "pass", f"{label} (cache hit)", "", run_id)
            return ToolResult(True, cached[0], None, tool, ms(), cached=True)

        def stale_or(err: ToolError) -> ToolResult:
            if cached:  # stale-while-error: an old answer beats no answer
                self.sink.emit(f"tool:{tool}", tool, 0, "fallback", ms(), "pass", f"{label} (stale cache)", f"[{err.kind}] {err.message}", run_id)
                return ToolResult(True, cached[0], None, tool, ms(), cached=True, stale=True)
            return finish_fail(err)

        # 3. circuit breaker and budget
        br = self._breaker(tool)
        left = br.open_remaining()
        if left > 0 and mode is None:
            return stale_or(ToolError("circuit_open", f"{tool} failed {br.consecutive} times in a row; skipped for another {left:.0f}s", True))
        if not self.budgets.allow(sp):
            return stale_or(ToolError("budget", f"{tool}: per-notebook limit of {self.budgets.limit(sp)} calls reached"))

        # 4. attempts
        last: Optional[ToolFailure] = None
        for attempt in range(sp.retries + 1):
            if not self._bucket(tool).acquire(timeout=3.0):
                last = ToolFailure("rate_limit", f"{tool}: local rate limit ({sp.rate_per_s}/s)", True)
                break
            self.budgets.charge(sp)
            st.calls += 1
            try:
                if mode == "fail":
                    raise ToolFailure("chaos", "chaos: forced failure (AASAAN_CHAOS)", False)
                if mode == "timeout":
                    raise ToolFailure("timeout", "chaos: forced timeout (AASAAN_CHAOS)", True)
                if mode == "rate_limit":
                    raise ToolFailure("rate_limit", "chaos: forced 429 (AASAAN_CHAOS)", True, status=429, retry_after=0)
                data = fn()
                if mode == "empty":
                    data = ""
                self._check(data, validate, min_chars)
            except ToolFailure as f:
                last = f
            except httpx.TimeoutException as e:
                last = ToolFailure("timeout", f"{type(e).__name__}: {e}", True)
            except httpx.HTTPError as e:
                last = ToolFailure("server", f"{type(e).__name__}: {e}", True)
            except Exception as e:  # a bug in a tool function must never take the pipeline down
                last = ToolFailure("server", f"{type(e).__name__}: {e}", False)
            else:
                br.record_success()
                st.last_success_ts, st.last_ok = time.time(), True
                if cache_key and sp.cache_ttl_s:
                    self.cache.set(cache_key, data)
                self.sink.emit(f"tool:{tool}", tool, attempt, "ok", ms(), "pass", label, "", run_id)
                return ToolResult(True, data, None, tool, ms())
            # failed attempt
            will_retry = last.retryable and attempt < sp.retries and last.status not in sp.disable_on_status
            self.sink.emit(f"tool:{tool}", tool, attempt, "retry" if will_retry else "fallback", ms(), "fail", label, f"[{last.kind}] {last.message}", run_id)
            if last.status in sp.disable_on_status:
                self.disable(tool, f"HTTP {last.status}")
                break
            if not will_retry:
                break
            self.sleep(backoff_delay(attempt, retry_after=last.retry_after))

        assert last is not None
        outage = last.kind in OUTAGE_KINDS   # a thin page or a 404 video is about that page, not about the tool being down
        if outage and br.record_failure():
            log.warning("[aasaan] circuit OPEN for tool %s after %d consecutive failures; skipping it for %.0fs", tool, br.consecutive, br.cooldown_s)
        st.errors += 1
        st.last_error, st.last_error_kind, st.last_error_ts = scrub(last.message)[:300], last.kind, time.time()
        if outage:
            st.last_ok = False
        if cached:
            self.sink.emit(f"tool:{tool}", tool, 0, "fallback", ms(), "pass", f"{label} (stale cache)", f"[{last.kind}] {last.message}", run_id)
            return ToolResult(True, cached[0], None, tool, ms(), cached=True, stale=True)
        return ToolResult(False, None, ToolError(last.kind, scrub(last.message)[:300], last.retryable), tool, ms())

    @staticmethod
    def _check(data: Any, validate: Optional[Callable[[Any], None]], min_chars: Optional[int]) -> None:
        if data is None or data == "" or data == [] or data == {}:
            raise ToolFailure("invalid", "empty result", False)
        if min_chars is not None and isinstance(data, str) and len(data) < min_chars:
            raise ToolFailure("thin", f"only {len(data)} characters of content (need {min_chars})", False)
        if validate is not None:
            try:
                validate(data)
            except ToolFailure:
                raise
            except Exception as e:
                raise ToolFailure("invalid", f"output failed validation: {e}", False)

    # ---- health
    def health(self) -> dict:
        now, out = time.time(), {}
        for name, sp in self.specs.items():
            st, br = self._st(name), self._breaker(name)
            keys = [mask_key(sp.key_env)] if sp.key_env else []
            keys += [mask_key(e) for e in sp.extra_keys]
            if name in self._disabled:
                state, reason = "disabled", f"disabled for this session ({self._disabled[name]})"
            elif sp.key_required and sp.key_env and not os.environ.get(sp.key_env):
                state, reason = "disabled", f"{sp.key_env} is not set"
            elif not sp.integrated:
                state, reason = "disabled", "not integrated in this codebase"
            elif br.open_remaining() > 0:
                state, reason = "open_circuit", f"skipped for another {br.open_remaining():.0f}s"
            elif st.last_ok is False or br.consecutive > 0:
                state, reason = "degraded", st.last_error
            else:
                state, reason = "ok", ""
            nb = current_notebook.get()
            out[name] = {"state": state, "reason": reason, "description": sp.description, "keys": keys,
                         "last_error": st.last_error, "last_error_kind": st.last_error_kind, "last_error_at": st.last_error_ts or None,
                         "last_success_at": st.last_success_ts or None, "calls": st.calls, "errors": st.errors, "cache_hits": st.cache_hits,
                         "budget_per_notebook": self.budgets.limit(sp), "chaos": self.chaos.mode_for("tool", name)}
        for name, check in self._local.items():
            try:
                out[name] = {"state": "ok", "kind": "local", **check()}
            except Exception as e:
                out[name] = {"state": "degraded", "kind": "local", "reason": scrub(e)[:200]}
        return out
