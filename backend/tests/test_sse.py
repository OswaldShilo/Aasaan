import asyncio
import json
import time
import pytest
from starlette.testclient import TestClient
from app.main import create_app
from app.sse import StreamHub, get_sse_settings
from fake_provider import FakeProvider


@pytest.fixture
def app_and_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AASAAN_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("AASAAN_UPLOADS", str(tmp_path / "uploads"))
    monkeypatch.setenv("AASAAN_MODE", "mock")
    # Default fast timings for test suite
    monkeypatch.setenv("AASAAN_SSE_MAX_SECONDS", "3.0")
    monkeypatch.setenv("AASAAN_SSE_HEARTBEAT_SEC", "0.2")
    monkeypatch.setenv("AASAAN_SSE_POLL_INTERVAL", "0.05")

    app = create_app(start_worker=False, self_test_on_start=False, provider_override=FakeProvider())
    client = TestClient(app)
    return app, client


def _create_notebook(client: TestClient) -> str:
    lid = client.post("/api/learners").json()["learner_id"]
    res = client.post("/api/notebooks", data={"learner_id": lid, "text": "I want to learn DSA", "language": "en"})
    assert res.status_code == 200, res.text
    return res.json()["notebook_id"]


def test_events_carry_monotonic_ids(app_and_client):
    app, client = app_and_client
    nid = _create_notebook(client)

    with client.stream("GET", f"/api/notebooks/{nid}/stream") as resp:
        assert resp.status_code == 200
        lines = []
        for line in resp.iter_lines():
            if line:
                lines.append(line)
            if any("id: " in l for l in lines):
                break

    id_lines = [l for l in lines if l.startswith("id: ")]
    assert len(id_lines) >= 1
    eid = int(id_lines[0].replace("id: ", "").strip())
    assert eid >= 1


def test_heartbeat_comments_on_idle_stream(app_and_client, monkeypatch):
    monkeypatch.setenv("AASAAN_SSE_HEARTBEAT_SEC", "0.1")
    monkeypatch.setenv("AASAAN_SSE_MAX_SECONDS", "0.8")
    app, client = app_and_client
    nid = _create_notebook(client)

    keepalives = 0
    with client.stream("GET", f"/api/notebooks/{nid}/stream") as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line == ": keepalive":
                keepalives += 1
                if keepalives >= 2:
                    break

    assert keepalives >= 2


def test_planned_close_at_max_seconds_with_retry(app_and_client, monkeypatch):
    monkeypatch.setenv("AASAAN_SSE_MAX_SECONDS", "0.4")
    monkeypatch.setenv("AASAAN_SSE_HEARTBEAT_SEC", "1.0")
    app, client = app_and_client
    nid = _create_notebook(client)

    t0 = time.monotonic()
    all_lines = []
    with client.stream("GET", f"/api/notebooks/{nid}/stream") as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line:
                all_lines.append(line)
    elapsed = time.monotonic() - t0

    # Stream closed by itself in ~0.4s
    assert elapsed < 2.0
    # Both start and end include retry: 2000
    retry_lines = [l for l in all_lines if l.startswith("retry: ")]
    assert len(retry_lines) >= 2
    assert retry_lines[-1] == "retry: 2000"


def test_reconnect_with_last_event_id_resumes_without_gaps_or_duplicates(app_and_client):
    app, client = app_and_client
    nid = _create_notebook(client)
    hub: StreamHub = app.state.sse_hub

    # Populate state buffer manually with events 1, 2, 3
    state = asyncio.run(hub.get_or_create_state(nid))
    state.current_id = 3
    state.last_payload = json.dumps({"snap": 3})
    state.buffer.clear()
    state.buffer.append((1, json.dumps({"snap": 1})))
    state.buffer.append((2, json.dumps({"snap": 2})))
    state.buffer.append((3, json.dumps({"snap": 3})))

    # Reconnect with Last-Event-ID: 1 -> should replay events 2 and 3
    replayed_ids = []
    with client.stream("GET", f"/api/notebooks/{nid}/stream", headers={"Last-Event-ID": "1"}) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line.startswith("id: "):
                replayed_ids.append(int(line.replace("id: ", "").strip()))
                if len(replayed_ids) == 2:
                    break

    assert replayed_ids == [2, 3]

    # Reconnect via query parameter ?last_event_id=2 -> should replay event 3 only
    replayed_ids_query = []
    with client.stream("GET", f"/api/notebooks/{nid}/stream?last_event_id=2") as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line.startswith("id: "):
                replayed_ids_query.append(int(line.replace("id: ", "").strip()))
                if len(replayed_ids_query) == 1:
                    break

    assert replayed_ids_query == [3]


def test_reconnect_older_than_buffer_returns_resync(app_and_client):
    app, client = app_and_client
    nid = _create_notebook(client)
    hub: StreamHub = app.state.sse_hub

    # Set buffer to only hold 2 items: 10 and 11
    state = asyncio.run(hub.get_or_create_state(nid, buffer_size=2))
    state.current_id = 11
    state.last_payload = json.dumps({"snap": 11})
    state.buffer.clear()
    state.buffer.append((10, json.dumps({"snap": 10})))
    state.buffer.append((11, json.dumps({"snap": 11})))

    # Client reconnects with last_event_id = 5 (older than buffer start 10)
    lines = []
    with client.stream("GET", f"/api/notebooks/{nid}/stream", headers={"Last-Event-ID": "5"}) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line:
                lines.append(line)
            if "event: resync" in lines:
                break

    assert "event: resync" in lines


def test_client_disconnect_cleans_up_producers_and_tasks(app_and_client):
    app, client = app_and_client
    nid = _create_notebook(client)
    hub: StreamHub = app.state.sse_hub
    engine = app.state.engine

    from starlette.requests import Request
    scope = {"type": "http", "method": "GET", "path": f"/api/notebooks/{nid}/stream", "headers": [], "query_string": b""}
    async def receive():
        return {"type": "http.request"}
    req = Request(scope, receive)

    async def run_disconnect_check():
        state = await hub.get_or_create_state(nid)
        assert len(state.subscribers) == 0

        gen = hub.subscribe(nid, engine, req)
        c1 = await gen.asend(None)
        assert "retry: 2000" in c1
        # While connected, subscriber is registered and poll_task is active
        assert len(state.subscribers) == 1
        assert state.poll_task is not None and not state.poll_task.done()

        # Simulate client disconnect by closing the generator
        await gen.aclose()

        # After client disconnect, subscriber is unregistered and poll_task is cancelled
        assert len(state.subscribers) == 0
        assert state.poll_task is None or state.poll_task.done()

    asyncio.run(run_disconnect_check())


def test_gzip_middleware_does_not_buffer_or_compress_sse(app_and_client):
    app, client = app_and_client
    nid = _create_notebook(client)

    with client.stream("GET", f"/api/notebooks/{nid}/stream", headers={"Accept-Encoding": "gzip"}) as resp:
        assert resp.status_code == 200
        content_type = resp.headers.get("content-type", "")
        content_encoding = resp.headers.get("content-encoding")

        assert "text/event-stream" in content_type
        # GZipMiddleware must NOT compress or buffer text/event-stream
        assert content_encoding is None or content_encoding != "gzip"

        # Verify stream chunks arrive immediately
        received = []
        for line in resp.iter_lines():
            if line:
                received.append(line)
            if any(l.startswith("id: ") for l in received):
                break

        assert "retry: 2000" in received
        assert any(l.startswith("id: ") for l in received)


def test_server_shutdown_terminates_stream_with_retry(app_and_client):
    app, client = app_and_client
    nid = _create_notebook(client)
    hub: StreamHub = app.state.sse_hub

    # Connect client in a background thread or stream iteration
    with client.stream("GET", f"/api/notebooks/{nid}/stream") as resp:
        # Trigger shutdown
        asyncio.run(hub.shutdown())
        lines = []
        for line in resp.iter_lines():
            if line:
                lines.append(line)

    assert "retry: 2000" in lines
