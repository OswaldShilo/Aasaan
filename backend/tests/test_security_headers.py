"""Tests for security headers middleware, CSP modes, HSTS, and CSP violation reporting."""
import json
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

from app.main import create_app
from app.security_headers import SecurityHeadersMiddleware, CSP_POLICY, get_csp_mode


@pytest.fixture
def client(tmp_path):
    db_file = tmp_path / "test.db"
    uploads = tmp_path / "uploads"
    app = create_app(db_path=str(db_file), upload_dir=str(uploads), start_worker=False, self_test_on_start=False)
    return TestClient(app)


def test_every_expected_header_is_present_on_normal_route(client: TestClient):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"
    assert resp.headers.get("Permissions-Policy") == "camera=(), microphone=(), geolocation=()"
    assert "server" not in resp.headers
    # Default CSP is report-only
    assert "Content-Security-Policy-Report-Only" in resp.headers
    assert "Content-Security-Policy" not in resp.headers


def test_security_headers_present_on_404_and_static_file(client: TestClient):
    # 404 route
    resp_404 = client.get("/api/nonexistent_missing_endpoint")
    assert resp_404.status_code == 404
    assert resp_404.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp_404.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert resp_404.headers.get("X-Frame-Options") == "SAMEORIGIN"

    # Static file route
    resp_static = client.get("/static/style.css")
    assert resp_static.status_code == 200
    assert resp_static.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp_static.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert resp_static.headers.get("X-Frame-Options") == "SAMEORIGIN"


def test_security_headers_present_on_500_error(tmp_path):
    # App with custom 500 handler to test error condition
    test_app = FastAPI()
    test_app.add_middleware(SecurityHeadersMiddleware)

    @test_app.get("/boom")
    def boom():
        raise RuntimeError("simulated server error")

    @test_app.exception_handler(Exception)
    def handle_err(_, __):
        return JSONResponse({"error": "server error"}, status_code=500)

    t_client = TestClient(test_app, raise_server_exceptions=False)
    resp = t_client.get("/boom")
    assert resp.status_code == 500
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"
    assert resp.headers.get("Permissions-Policy") == "camera=(), microphone=(), geolocation=()"


def test_hsts_only_appears_when_x_forwarded_proto_is_https(client: TestClient):
    # Plain HTTP: no HSTS
    resp_http = client.get("/api/health")
    assert "Strict-Transport-Security" not in resp_http.headers

    # Explicit HTTP proto: no HSTS
    resp_proto_http = client.get("/api/health", headers={"X-Forwarded-Proto": "http"})
    assert "Strict-Transport-Security" not in resp_proto_http.headers

    # HTTPS forwarded proto: HSTS must appear
    resp_https = client.get("/api/health", headers={"X-Forwarded-Proto": "https"})
    assert "Strict-Transport-Security" in resp_https.headers
    assert "max-age=31536000" in resp_https.headers["Strict-Transport-Security"]
    assert "includeSubDomains" in resp_https.headers["Strict-Transport-Security"]


def test_csp_mode_switch(tmp_path, monkeypatch: pytest.MonkeyPatch):
    db_file = tmp_path / "test_csp.db"
    uploads = tmp_path / "uploads_csp"

    # 1. Report-only mode (default)
    monkeypatch.setenv("AASAAN_CSP_MODE", "report-only")
    app1 = create_app(db_path=str(db_file), upload_dir=str(uploads), start_worker=False, self_test_on_start=False)
    c1 = TestClient(app1)
    r1 = c1.get("/api/health")
    assert "Content-Security-Policy-Report-Only" in r1.headers
    assert "Content-Security-Policy" not in r1.headers

    # 2. Enforce mode
    monkeypatch.setenv("AASAAN_CSP_MODE", "enforce")
    app2 = create_app(db_path=str(db_file), upload_dir=str(uploads), start_worker=False, self_test_on_start=False)
    c2 = TestClient(app2)
    r2 = c2.get("/api/health")
    assert "Content-Security-Policy" in r2.headers
    assert "Content-Security-Policy-Report-Only" not in r2.headers

    # 3. Off mode
    monkeypatch.setenv("AASAAN_CSP_MODE", "off")
    app3 = create_app(db_path=str(db_file), upload_dir=str(uploads), start_worker=False, self_test_on_start=False)
    c3 = TestClient(app3)
    r3 = c3.get("/api/health")
    assert "Content-Security-Policy" not in r3.headers
    assert "Content-Security-Policy-Report-Only" not in r3.headers


def test_csp_frame_src_includes_youtube_nocookie():
    assert "frame-src" in CSP_POLICY
    assert "https://www.youtube-nocookie.com" in CSP_POLICY
    assert "https://www.youtube.com" in CSP_POLICY


def test_csp_report_endpoint(client: TestClient):
    # 1. Valid report returns 204
    payload = {
        "csp-report": {
            "document-uri": "https://aasaan.example.com/test",
            "blocked-uri": "https://unauthorized.cdn.com/asset.js",
            "violated-directive": "script-src",
            "effective-directive": "script-src",
            "disposition": "report"
        }
    }
    resp = client.post("/csp-report", json=payload)
    assert resp.status_code == 204

    # 2. Garbage non-JSON input does not crash and returns 204
    resp_garbage = client.post(
        "/csp-report",
        content=b"\xff\xfe\x00\x00corrupted non utf-8 garbage \x00\x01\x02",
        headers={"Content-Type": "application/csp-report"}
    )
    assert resp_garbage.status_code == 204

    # 3. Oversized payload is rejected gracefully with 204
    oversized = b"a" * 25000
    resp_oversized = client.post(
        "/csp-report",
        content=oversized,
        headers={"Content-Type": "application/csp-report"}
    )
    assert resp_oversized.status_code == 204


def test_does_not_overwrite_deliberately_set_header():
    test_app = FastAPI()
    test_app.add_middleware(SecurityHeadersMiddleware)

    @test_app.get("/custom")
    def custom():
        return JSONResponse(
            {"custom": True},
            headers={"X-Frame-Options": "DENY", "X-Content-Type-Options": "custom-sniff"}
        )

    t_client = TestClient(test_app)
    resp = t_client.get("/custom")
    assert resp.status_code == 200
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert resp.headers.get("X-Content-Type-Options") == "custom-sniff"
    assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"


def test_sse_and_streaming_responses_work_with_middleware_on():
    test_app = FastAPI()
    test_app.add_middleware(SecurityHeadersMiddleware)

    @test_app.get("/stream")
    def stream():
        def generate():
            yield b"data: chunk1\n\n"
            yield b"data: chunk2\n\n"

        return StreamingResponse(generate(), media_type="text/event-stream")

    t_client = TestClient(test_app)
    with t_client.stream("GET", "/stream") as stream_resp:
        assert stream_resp.status_code == 200
        assert stream_resp.headers.get("X-Content-Type-Options") == "nosniff"
        lines = list(stream_resp.iter_lines())
        assert "data: chunk1" in lines
        assert "data: chunk2" in lines
