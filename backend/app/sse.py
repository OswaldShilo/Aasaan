"""Server-Sent Events (SSE) management for live notebook updates.

Provides monotonic event IDs, a bounded in-memory replay buffer, keepalive
heartbeats, planned stream closure before proxy/ACA timeouts (240s),
and seamless client resumption using Last-Event-ID (or query param).
"""

from __future__ import annotations

import asyncio
from collections import deque
import json
import logging
import os
import random
import time
from typing import Any, AsyncGenerator, Optional
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

log_ = logging.getLogger("aasaan.sse")


def get_sse_settings() -> dict[str, Any]:
    """Retrieve SSE operational parameters from environment variables."""
    return {
        "max_seconds": float(os.getenv("AASAAN_SSE_MAX_SECONDS", "200.0")),
        "heartbeat_sec": float(os.getenv("AASAAN_SSE_HEARTBEAT_SEC", "15.0")),
        "buffer_size": int(os.getenv("AASAAN_SSE_BUFFER_SIZE", "100")),
        "poll_interval": float(os.getenv("AASAAN_SSE_POLL_INTERVAL", "1.0")),
        "retry_ms": int(os.getenv("AASAAN_SSE_RETRY_MS", "2000")),
    }


class NotebookStreamState:
    """State and subscribers for a single active notebook stream."""

    def __init__(self, nid: str, buffer_size: int = 100):
        self.nid = nid
        self.current_id: int = 0
        self.buffer: deque[tuple[int, str]] = deque(maxlen=buffer_size)
        self.last_payload: str | None = None
        self.subscribers: set[asyncio.Queue] = set()
        self.poll_task: asyncio.Task | None = None
        self.lock = asyncio.Lock()


class StreamHub:
    """Centralized coordinator for notebook SSE streams."""

    def __init__(self):
        self._states: dict[str, NotebookStreamState] = {}
        self._global_lock = asyncio.Lock()

    async def get_or_create_state(self, nid: str, buffer_size: int = 100) -> NotebookStreamState:
        async with self._global_lock:
            if nid not in self._states:
                self._states[nid] = NotebookStreamState(nid, buffer_size=buffer_size)
            return self._states[nid]

    async def remove_notebook(self, nid: str) -> None:
        async with self._global_lock:
            state = self._states.pop(nid, None)
        if state:
            async with state.lock:
                for sub in list(state.subscribers):
                    try:
                        sub.put_nowait(None)
                    except Exception:
                        pass
                if state.poll_task and not state.poll_task.done():
                    state.poll_task.cancel()
                    state.poll_task = None

    async def shutdown(self) -> None:
        """Signal all active subscriber streams to cleanly terminate with retry hint."""
        async with self._global_lock:
            states = list(self._states.values())
        for state in states:
            async with state.lock:
                for sub in list(state.subscribers):
                    try:
                        sub.put_nowait(None)
                    except Exception:
                        pass
                if state.poll_task and not state.poll_task.done():
                    state.poll_task.cancel()
                    state.poll_task = None

    async def subscribe(
        self,
        nid: str,
        engine: Any,
        request: Request,
        last_event_id: Optional[int] = None,
        max_seconds: Optional[float] = None,
        heartbeat_sec: Optional[float] = None,
        poll_interval: Optional[float] = None,
        retry_ms: Optional[int] = None,
    ) -> AsyncGenerator[str, None]:
        """Yield SSE stream chunks for a client connection."""
        settings = get_sse_settings()
        cfg_max = max_seconds if max_seconds is not None else settings["max_seconds"]
        cfg_heartbeat = heartbeat_sec if heartbeat_sec is not None else settings["heartbeat_sec"]
        cfg_poll = poll_interval if poll_interval is not None else settings["poll_interval"]
        cfg_retry = retry_ms if retry_ms is not None else settings["retry_ms"]
        cfg_buf = settings["buffer_size"]

        # Parse Last-Event-ID if not explicitly provided as int
        if last_event_id is None:
            raw_id = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
            if raw_id:
                try:
                    last_event_id = int(raw_id)
                except ValueError:
                    last_event_id = None

        state = await self.get_or_create_state(nid, buffer_size=cfg_buf)
        queue: asyncio.Queue = asyncio.Queue(maxsize=cfg_buf)

        # Planned close calculation with small jitter to avoid thundering herd
        jitter = random.uniform(-5.0, 5.0) if cfg_max >= 60.0 else random.uniform(-0.02, 0.02) * cfg_max
        planned_duration = max(0.1, cfg_max + jitter)
        if planned_duration > 230.0:
            planned_duration = 230.0
        planned_close = time.monotonic() + planned_duration

        to_replay: list[str] = [f"retry: {cfg_retry}\n\n"]
        max_seen_id = 0

        async with state.lock:
            state.subscribers.add(queue)
            # If no cached snapshot exists, fetch initial snapshot synchronously in threadpool
            if state.last_payload is None:
                view = await run_in_threadpool(engine.notebook_view, nid)
                payload = json.dumps(view)
                state.current_id += 1
                state.last_payload = payload
                state.buffer.append((state.current_id, payload))

            min_buf_id = state.buffer[0][0] if state.buffer else 1
            max_buf_id = state.current_id

            if last_event_id is not None:
                # Check for missed range or mismatched replica restart
                if last_event_id + 1 < min_buf_id or last_event_id > max_buf_id:
                    to_replay.append("event: resync\ndata: {}\n\n")
                    max_seen_id = max_buf_id
                else:
                    for eid, p in state.buffer:
                        if eid > last_event_id:
                            to_replay.append(f"id: {eid}\ndata: {p}\n\n")
                            max_seen_id = max(max_seen_id, eid)
                    if max_seen_id == 0:
                        max_seen_id = last_event_id
            else:
                # Fresh connection: deliver the latest full snapshot immediately
                to_replay.append(f"id: {state.current_id}\ndata: {state.last_payload}\n\n")
                max_seen_id = state.current_id

            # Start shared poller if not already active
            if state.poll_task is None or state.poll_task.done():
                state.poll_task = asyncio.create_task(_poll_loop(state, engine, cfg_poll))

        # Deliver replay or initial snapshot, then run live loop
        try:
            for chunk in to_replay:
                yield chunk

            while True:
                if await request.is_disconnected():
                    break

                now = time.monotonic()
                remaining = planned_close - now
                if remaining <= 0:
                    yield f"retry: {cfg_retry}\n\n"
                    break

                wait_timeout = min(cfg_heartbeat, remaining)
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=wait_timeout)
                except asyncio.TimeoutError:
                    if time.monotonic() >= planned_close:
                        yield f"retry: {cfg_retry}\n\n"
                        break
                    yield ": keepalive\n\n"
                    continue

                if item is None:
                    # Server shutdown signal
                    yield f"retry: {cfg_retry}\n\n"
                    break

                _mtype, eid, payload = item
                if eid > max_seen_id:
                    max_seen_id = eid
                    yield f"id: {eid}\ndata: {payload}\n\n"
        finally:
            async with state.lock:
                state.subscribers.discard(queue)
                if not state.subscribers and state.poll_task and not state.poll_task.done():
                    state.poll_task.cancel()
                    state.poll_task = None


async def _poll_loop(state: NotebookStreamState, engine: Any, interval: float) -> None:
    """Shared single-task background poller for active notebook subscribers."""
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                view = await run_in_threadpool(engine.notebook_view, state.nid)
                payload = json.dumps(view)
            except Exception as e:
                log_.warning("[sse] poller error for notebook %s: %s", state.nid, e)
                continue

            if payload != state.last_payload:
                async with state.lock:
                    state.current_id += 1
                    eid = state.current_id
                    state.last_payload = payload
                    state.buffer.append((eid, payload))
                    for sub in list(state.subscribers):
                        try:
                            sub.put_nowait(("event", eid, payload))
                        except asyncio.QueueFull:
                            pass
    except asyncio.CancelledError:
        pass

