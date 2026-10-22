"""Tests for configurable paths, startup directory validation, and health/readiness endpoints."""
import os
import stat
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import (
    DEFAULT_DB_PATH,
    DEFAULT_UPLOADS_DIR,
    get_db_path,
    get_uploads_dir,
    validate_and_prepare_paths,
)
from tests.fake_provider import FakeProvider


def test_default_paths_fallback(monkeypatch):
    """Default paths match the expected data/aasaan.db and data/uploads locations."""
    monkeypatch.delenv("AASAAN_DB", raising=False)
    monkeypatch.delenv("AASAAN_UPLOADS", raising=False)
    assert get_db_path() == DEFAULT_DB_PATH.resolve()
    assert get_uploads_dir() == DEFAULT_UPLOADS_DIR.resolve()


def test_custom_env_paths(monkeypatch, tmp_path):
    """Paths configured via AASAAN_DB and AASAAN_UPLOADS env vars are honored."""
    custom_db = tmp_path / "custom" / "test.db"
    custom_up = tmp_path / "custom" / "files"
    monkeypatch.setenv("AASAAN_DB", str(custom_db))
    monkeypatch.setenv("AASAAN_UPLOADS", str(custom_up))

    assert get_db_path() == custom_db.resolve()
    assert get_uploads_dir() == custom_up.resolve()


def test_directories_auto_created_on_startup(tmp_path):
    """Startup validation creates parent directories for DB and uploads if absent."""
    db_file = tmp_path / "dir_a" / "dir_b" / "my.db"
    up_dir = tmp_path / "dir_c" / "dir_d"

    assert not db_file.parent.exists()
    assert not up_dir.exists()

    validate_and_prepare_paths(db_file, up_dir)

    assert db_file.parent.is_dir()
    assert up_dir.is_dir()


def test_fail_fast_on_invalid_path(tmp_path):
    """Invalid path settings (e.g. creating directory under a regular file) fail fast."""
    blocker = tmp_path / "file_blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    invalid_db = blocker / "sub" / "aasaan.db"
    valid_up = tmp_path / "valid_up"

    with pytest.raises((RuntimeError, OSError)):
        validate_and_prepare_paths(invalid_db, valid_up)

    invalid_up = blocker / "sub_up"
    with pytest.raises((RuntimeError, OSError)):
        validate_and_prepare_paths(tmp_path / "ok.db", invalid_up)


def test_fail_fast_on_unwritable_directory(tmp_path, monkeypatch):
    """Permission errors during directory write probes fail fast with clear errors."""
    unwritable = tmp_path / "unwritable"
    unwritable.mkdir(parents=True, exist_ok=True)

    # Simulate permission denied when writing probe file
    orig_write = Path.write_text

    def mock_write(self, *args, **kwargs):
        if "probe" in self.name:
            raise PermissionError("Simulated permission denied")
        return orig_write(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", mock_write)

    with pytest.raises(PermissionError, match="is not writable"):
        validate_and_prepare_paths(unwritable / "db.sqlite", tmp_path / "up")


def test_liveness_health_endpoint_fast_and_dependency_free(tmp_path):
    """GET /health must return 200 with {"ok": true} quickly without hitting DB or LLM."""
    app = create_app(
        db_path=str(tmp_path / "live.db"),
        provider_override=FakeProvider(),
        start_worker=False,
        upload_dir=str(tmp_path / "up"),
    )
    with TestClient(app) as client:
        t0 = time.perf_counter()
        res = client.get("/health")
        elapsed = time.perf_counter() - t0

        assert res.status_code == 200
        assert res.json() == {"ok": True}
        assert elapsed < 0.2  # fast in-memory response


def test_readiness_health_endpoint_success(tmp_path):
    """GET /health/ready returns 200 when database is writable."""
    app = create_app(
        db_path=str(tmp_path / "ready.db"),
        provider_override=FakeProvider(),
        start_worker=False,
        upload_dir=str(tmp_path / "up"),
    )
    with TestClient(app) as client:
        res = client.get("/health/ready")
        assert res.status_code == 200
        assert res.json() == {"ok": True}

        # Also available on /api/health/ready
        res_api = client.get("/api/health/ready")
        assert res_api.status_code == 200
        assert res_api.json() == {"ok": True}


def test_readiness_health_endpoint_failure_on_unwritable_db(tmp_path):
    """GET /health/ready returns 503 without leaking paths or secrets when DB is unwritable."""
    db_file = tmp_path / "readonly.db"
    app = create_app(
        db_path=str(db_file),
        provider_override=FakeProvider(),
        start_worker=False,
        upload_dir=str(tmp_path / "up"),
    )
    with TestClient(app) as client:
        # Initial check succeeds
        assert client.get("/health/ready").status_code == 200

        # Simulate database becoming read-only (e.g. disk mount changed to ro or SQLite read-only mode)
        app.state.store.conn.execute("PRAGMA query_only = 1")

        res = client.get("/health/ready")
        assert res.status_code == 503
        data = res.json()
        assert data["ok"] is False
        assert data["reason"] == "database is not writable"

        # Ensure no paths, directories or secrets leaked in response
        assert str(db_file) not in res.text
        assert str(tmp_path) not in res.text


def test_health_routes_require_no_auth_and_no_rate_limit(tmp_path, monkeypatch):
    """Neither /health nor /health/ready require admin token and neither is rate-limited."""
    monkeypatch.setenv("AASAAN_ADMIN_TOKEN", "supersecret123")
    app = create_app(
        db_path=str(tmp_path / "auth_test.db"),
        provider_override=FakeProvider(),
        start_worker=False,
        upload_dir=str(tmp_path / "up"),
    )
    with TestClient(app) as client:
        # Calling without x-admin-token header should succeed
        for _ in range(25):
            res_live = client.get("/health")
            assert res_live.status_code == 200
            res_ready = client.get("/health/ready")
            assert res_ready.status_code == 200


def test_aca_style_paths_configuration(tmp_path, monkeypatch):
    """App initializes cleanly when configured with Azure Container Apps paths."""
    aca_db_dir = tmp_path / "local"
    aca_db = aca_db_dir / "aasaan.db"
    aca_uploads = tmp_path / "mnt" / "files"

    monkeypatch.setenv("AASAAN_DB", str(aca_db))
    monkeypatch.setenv("AASAAN_UPLOADS", str(aca_uploads))

    app = create_app(provider_override=FakeProvider(), start_worker=False)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/health/ready").status_code == 200
        assert aca_db.exists()
        assert aca_uploads.is_dir()
