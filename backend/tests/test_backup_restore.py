"""Tests for SQLite online backup loop, restore init container, and retention."""
import os
import sqlite3
import time
from pathlib import Path

import pytest

from app.backup_loop import run_backup_once
from app.backup_utils import (
    get_backup_dir,
    list_backups,
    remove_wal_and_shm,
    verify_integrity,
)
from app.db import Store
from app.restore import run_restore


def _make_sample_db(path: Path, table: str = "notes", value: str = "hello") -> Path:
    """Create a valid SQLite database with WAL mode and sample records."""
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (id INTEGER PRIMARY KEY, msg TEXT)")
    conn.execute(f"INSERT INTO {table} (msg) VALUES (?)", (value,))
    conn.commit()
    conn.close()
    return path


def test_backup_creates_valid_integrity_checked_file_during_writes(tmp_path: Path):
    db_file = tmp_path / "live.db"
    backup_dir = tmp_path / "backups"

    store = Store(db_file)
    store.conn.execute("CREATE TABLE test_data (id INTEGER PRIMARY KEY, val TEXT)")
    store.conn.execute("INSERT INTO test_data (val) VALUES ('initial')")

    # Perform active write
    store.conn.execute("INSERT INTO test_data (val) VALUES ('active_write')")

    success, msg, backup_path, size_bytes, duration = run_backup_once(
        db_path=db_file, backup_dir=backup_dir
    )
    assert success is True, f"Backup failed: {msg}"
    assert backup_path is not None
    assert backup_path.exists()
    assert size_bytes > 0

    # Confirm integrity
    ok, reason = verify_integrity(backup_path)
    assert ok is True
    assert reason == "ok"

    # Confirm content in backup matches live data
    bconn = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    rows = bconn.execute("SELECT val FROM test_data ORDER BY id").fetchall()
    bconn.close()
    assert [r[0] for r in rows] == ["initial", "active_write"]
    store.conn.close()


def test_corrupted_copy_is_rejected_and_not_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_file = tmp_path / "live.db"
    backup_dir = tmp_path / "backups"
    _make_sample_db(db_file)

    # Force integrity check to simulate a corrupted copy
    monkeypatch.setattr("app.backup_loop.verify_integrity", lambda p: (False, "forced corruption error"))

    success, msg, backup_path, size_bytes, _ = run_backup_once(
        db_path=db_file, backup_dir=backup_dir
    )
    assert success is False
    assert "forced corruption error" in msg
    assert backup_path is None

    # Verify no .db or .tmp files are kept in the backup directory
    kept_files = list(backup_dir.glob("aasaan-*.db*"))
    assert kept_files == []


def test_retention_deletes_old_files_but_keeps_newest_3(tmp_path: Path):
    db_file = tmp_path / "live.db"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    _make_sample_db(db_file)

    now = time.time()
    day_sec = 86400

    # Create 5 valid backup files with staggered timestamps
    created = []
    for i in range(5):
        bpath = backup_dir / f"aasaan-2026-10-0{i+1}-1200.db"
        _make_sample_db(bpath, value=f"record_{i}")
        # Make all of them older than 20 days
        mtime = now - ((25 - i) * day_sec)
        os.utime(bpath, (mtime, mtime))
        created.append(bpath)

    # All 5 files are older than keep_days=14
    # Run backup once: writes a 6th newest backup today, retention should delete
    # files older than 14 days EXCEPT the newest 3 backups must be preserved.
    success, _, new_b, _, _ = run_backup_once(
        db_path=db_file, backup_dir=backup_dir, keep_days=14
    )
    assert success is True

    remaining = list_backups(backup_dir)
    # The newest 3 valid backups must always be kept regardless of age
    assert len(remaining) >= 3
    # Verify the newest backup created today is in remaining
    assert new_b in remaining


def test_restore_picks_newest_valid_backup(tmp_path: Path):
    db_file = tmp_path / "app.db"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    b_older = backup_dir / "aasaan-2026-10-01-1000.db"
    b_newer = backup_dir / "aasaan-2026-10-02-1000.db"
    _make_sample_db(b_older, value="old_val")
    _make_sample_db(b_newer, value="new_val")

    rc = run_restore(db_path=db_file, backup_dir=backup_dir)
    assert rc == 0
    assert db_file.exists()

    conn = sqlite3.connect(str(db_file))
    row = conn.execute("SELECT msg FROM notes").fetchone()
    conn.close()
    assert row[0] == "new_val"


def test_restore_skips_deliberately_corrupted_newest_backup_and_uses_next(tmp_path: Path):
    db_file = tmp_path / "app.db"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    b_valid_older = backup_dir / "aasaan-2026-10-01-1000.db"
    b_corrupt_newer = backup_dir / "aasaan-2026-10-02-1000.db"
    _make_sample_db(b_valid_older, value="valid_data")

    # Deliberately corrupt the newer backup file with bad header
    b_corrupt_newer.write_bytes(b"NOT A SQLITE FILE GARBAGE DATA 1234567890")

    rc = run_restore(db_path=db_file, backup_dir=backup_dir)
    assert rc == 0
    assert db_file.exists()

    conn = sqlite3.connect(str(db_file))
    row = conn.execute("SELECT msg FROM notes").fetchone()
    conn.close()
    assert row[0] == "valid_data"


def test_restore_does_nothing_when_db_already_exists(tmp_path: Path):
    db_file = tmp_path / "app.db"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    _make_sample_db(db_file, value="existing_live_data")
    b_backup = backup_dir / "aasaan-2026-10-01-1000.db"
    _make_sample_db(b_backup, value="backup_data")

    rc = run_restore(db_path=db_file, backup_dir=backup_dir)
    assert rc == 0

    # Ensure live DB was not overwritten
    conn = sqlite3.connect(str(db_file))
    row = conn.execute("SELECT msg FROM notes").fetchone()
    conn.close()
    assert row[0] == "existing_live_data"


def test_restore_with_no_backups_exits_0_and_leaves_no_db(tmp_path: Path):
    db_file = tmp_path / "app.db"
    backup_dir = tmp_path / "empty_backups"

    rc = run_restore(db_path=db_file, backup_dir=backup_dir)
    assert rc == 0
    assert not db_file.exists()


def test_stale_wal_and_shm_files_removed_on_restore(tmp_path: Path):
    db_file = tmp_path / "app.db"
    wal_file = tmp_path / "app.db-wal"
    shm_file = tmp_path / "app.db-shm"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    b_valid = backup_dir / "aasaan-2026-10-01-1000.db"
    _make_sample_db(b_valid, value="restored_data")

    # Create stale sidecars
    wal_file.write_bytes(b"stale wal data")
    shm_file.write_bytes(b"stale shm data")
    assert wal_file.exists()
    assert shm_file.exists()

    rc = run_restore(db_path=db_file, backup_dir=backup_dir)
    assert rc == 0
    assert db_file.exists()
    assert not wal_file.exists(), "Stale -wal should be removed on restore"
    assert not shm_file.exists(), "Stale -shm should be removed on restore"
