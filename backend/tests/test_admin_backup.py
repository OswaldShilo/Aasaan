"""Tests for POST /api/admin/backup endpoint."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from fake_provider import FakeProvider


@pytest.fixture
def test_setup(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    monkeypatch.setenv("AASAAN_DB", str(db_file))
    monkeypatch.setenv("AASAAN_BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("AASAAN_ADMIN_TOKEN", "secret-test-token")
    monkeypatch.setenv("AASAAN_BACKUP_PER_HOUR", "3")
    app = create_app(
        db_path=str(db_file),
        provider_override=FakeProvider(),
        upload_dir=str(tmp_path / "up"),
        start_worker=False,
    )
    with TestClient(app) as client:
        yield client, db_file, backup_dir


def test_admin_backup_unset_token_refuses(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.delenv("AASAAN_ADMIN_TOKEN", raising=False)
    app = create_app(
        db_path=str(db_file),
        provider_override=FakeProvider(),
        upload_dir=str(tmp_path / "up"),
        start_worker=False,
    )
    with TestClient(app) as client:
        resp = client.post("/api/admin/backup", headers={"X-Admin-Token": "some-token"})
        assert resp.status_code in (403, 503)
        assert "not configured" in resp.json().get("detail", "").lower() or "not set" in resp.json().get("detail", "").lower()


def test_admin_backup_no_token_header(test_setup):
    client, _, _ = test_setup
    resp = client.post("/api/admin/backup")
    assert resp.status_code == 403
    assert "token" in resp.json().get("detail", "").lower()


def test_admin_backup_wrong_token(test_setup):
    client, _, _ = test_setup
    resp = client.post("/api/admin/backup", headers={"X-Admin-Token": "wrong-token"})
    assert resp.status_code == 403
    assert "token" in resp.json().get("detail", "").lower()


def test_admin_backup_correct_token_success(test_setup):
    client, db_file, backup_dir = test_setup
    resp = client.post("/api/admin/backup", headers={"X-Admin-Token": "secret-test-token"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert isinstance(data["file"], str)
    assert data["file"].endswith(".db")
    # Verify no raw paths or secrets are returned in the response
    assert "/" not in data["file"]
    assert "\\" not in data["file"]
    assert str(backup_dir) not in str(data)
    assert "secret-test-token" not in str(data)
    assert data["size"] > 0
    assert isinstance(data["duration"], (int, float))
    assert (backup_dir / data["file"]).exists()


def test_admin_backup_rate_limit(test_setup):
    client, _, _ = test_setup
    # Configured to 3 per hour in test_setup
    for _ in range(3):
        resp = client.post("/api/admin/backup", headers={"X-Admin-Token": "secret-test-token"})
        assert resp.status_code == 200

    # 4th request should hit rate limit
    resp = client.post("/api/admin/backup", headers={"X-Admin-Token": "secret-test-token"})
    assert resp.status_code == 429
    assert "too many" in resp.json().get("detail", "").lower()


def test_admin_backup_failed_backup_clean_error(test_setup):
    client, _, _ = test_setup
    with patch("app.main.run_backup_once", return_value=(False, "disk full or read only", None, 0, 0.05)):
        resp = client.post("/api/admin/backup", headers={"X-Admin-Token": "secret-test-token"})
        assert resp.status_code == 500
        data = resp.json()
        assert data["ok"] is False
        assert "disk full or read only" in data["error"]
        assert data["file"] is None
        assert data["size"] == 0
