"""SQLite online backup loop for Azure Container Apps / Docker deployments.

Runs as: python -m app.backup_loop
Can also be called via run_backup_once() from admin endpoints.
"""
from __future__ import annotations

import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

# Ensure backend package can be imported if run directly as script
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

try:
    from .backup_utils import (
        check_dir_writable,
        get_backup_dir,
        list_backups,
        verify_integrity,
    )
    from .settings import get_db_path
except ImportError:
    from app.backup_utils import (
        check_dir_writable,
        get_backup_dir,
        list_backups,
        verify_integrity,
    )
    from app.settings import get_db_path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("aasaan.backup")

_shutdown_event = threading.Event()


def _handle_exit(signum, frame) -> None:
    logger.info("Received signal %s; shutting down backup loop cleanly...", signum)
    _shutdown_event.set()


def run_backup_once(
    db_path: Optional[Union[str, Path]] = None,
    backup_dir: Optional[Union[str, Path]] = None,
    keep_days: Optional[int] = None,
) -> tuple[bool, str, Optional[Path], int, float]:
    """Execute a single online backup with integrity verification and retention cleanup.

    Returns:
        (success: bool, status_msg: str, backup_file: Optional[Path], size_bytes: int, duration_sec: float)
    """
    start_time = time.time()
    now_utc = datetime.now(timezone.utc)
    ts_log = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    # 1. Resolve paths and settings
    resolved_db = get_db_path(db_path)
    if str(resolved_db) == ":memory:":
        duration = time.time() - start_time
        msg = "cannot backup in-memory database"
        logger.warning("[%s] result=skipped file=none size=0B duration=%.3fs (%s)", ts_log, duration, msg)
        return False, msg, None, 0, duration

    db_p = Path(resolved_db).resolve()
    resolved_backup_dir = get_backup_dir(backup_dir)

    # 2. Check if live DB exists
    if not db_p.exists():
        duration = time.time() - start_time
        msg = f"live database does not exist yet at {db_p.name}"
        logger.info("[%s] result=waiting file=none size=0B duration=%.3fs (%s)", ts_log, duration, msg)
        return False, msg, None, 0, duration

    # 3. Verify backup directory exists and is writable (UID 10001 check)
    try:
        bdir = check_dir_writable(resolved_backup_dir, name="Backup")
    except Exception as exc:
        duration = time.time() - start_time
        logger.error("[%s] result=error file=none size=0B duration=%.3fs (backup directory unwritable: %s)", ts_log, duration, exc)
        return False, str(exc), None, 0, duration

    # 4. Generate candidate filename (aasaan-YYYY-MM-DD-HHMM.db)
    base_stem = f"aasaan-{now_utc.strftime('%Y-%m-%d-%H%M')}"
    candidate_name = f"{base_stem}.db"
    if (bdir / candidate_name).exists():
        # Avoid collisions if triggered multiple times in one minute
        candidate_name = f"aasaan-{now_utc.strftime('%Y-%m-%d-%H%M%S')}.db"

    final_path = bdir / candidate_name
    tmp_path = bdir / f"{candidate_name}.tmp"

    # Remove any preexisting tmp file
    tmp_path.unlink(missing_ok=True)

    # 5. Perform online backup using sqlite3.Connection.backup
    # NEVER copy live .db, -wal, or -shm files directly.
    try:
        src_conn = sqlite3.connect(f"file:{db_p}?mode=ro", uri=True, timeout=15)
        try:
            dest_conn = sqlite3.connect(str(tmp_path))
            try:
                src_conn.backup(dest_conn)
            finally:
                dest_conn.close()
        finally:
            src_conn.close()
    except Exception as exc:
        duration = time.time() - start_time
        tmp_path.unlink(missing_ok=True)
        logger.error("[%s] result=error file=%s size=0B duration=%.3fs (online backup failed: %s)", ts_log, candidate_name, duration, exc)
        return False, str(exc), None, 0, duration

    # 6. Verify integrity of the backup copy
    ok, integrity_reason = verify_integrity(tmp_path)
    if not ok:
        duration = time.time() - start_time
        tmp_path.unlink(missing_ok=True)
        logger.error("[%s] result=integrity_failed file=%s size=0B duration=%.3fs (reason: %s)", ts_log, candidate_name, duration, integrity_reason)
        return False, f"integrity check failed: {integrity_reason}", None, 0, duration

    # 7. Atomic rename
    try:
        tmp_path.replace(final_path)
        size_bytes = final_path.stat().st_size
    except Exception as exc:
        duration = time.time() - start_time
        tmp_path.unlink(missing_ok=True)
        logger.error("[%s] result=error file=%s size=0B duration=%.3fs (atomic rename failed: %s)", ts_log, candidate_name, duration, exc)
        return False, str(exc), None, 0, duration

    # 8. Retention cleanup
    if keep_days is None:
        try:
            keep_days = int(os.environ.get("AASAAN_BACKUP_KEEP_DAYS", "14"))
        except ValueError:
            keep_days = 14

    pruned = _enforce_retention(bdir, keep_days=keep_days, now_ts=now_utc.timestamp())

    duration = time.time() - start_time
    logger.info(
        "[%s] result=ok file=%s size=%dB duration=%.3fs (pruned=%d)",
        ts_log,
        final_path.name,
        size_bytes,
        duration,
        pruned,
    )
    return True, "ok", final_path, size_bytes, duration


def _enforce_retention(backup_dir: Path, keep_days: int, now_ts: float) -> int:
    """Delete backups older than keep_days, but ALWAYS preserve newest 3 valid backups."""
    backups = list_backups(backup_dir)
    if len(backups) <= 3:
        return 0

    max_age_sec = keep_days * 86400
    pruned_count = 0

    # Backups are sorted newest first. The first 3 are protected.
    for old_backup in backups[3:]:
        try:
            age_sec = now_ts - old_backup.stat().st_mtime
            if age_sec > max_age_sec:
                old_backup.unlink(missing_ok=True)
                pruned_count += 1
                logger.info("Retention: deleted expired backup %s (age=%.1fdays)", old_backup.name, age_sec / 86400)
        except Exception as exc:
            logger.warning("Retention: error inspecting %s: %s", old_backup.name, exc)

    return pruned_count


def main() -> None:
    """Run continuous backup loop."""
    signal.signal(signal.SIGTERM, _handle_exit)
    signal.signal(signal.SIGINT, _handle_exit)

    try:
        interval_min = int(os.environ.get("AASAAN_BACKUP_EVERY_MIN", "5"))
    except ValueError:
        interval_min = 5

    logger.info("Starting Aasaan SQLite backup loop (interval=%dm, keep_days=%s)", interval_min, os.environ.get("AASAAN_BACKUP_KEEP_DAYS", "14"))

    while not _shutdown_event.is_set():
        try:
            run_backup_once()
        except Exception as exc:
            logger.error("Unexpected error in backup loop: %s", exc, exc_info=True)

        # Wait for interval or until SIGTERM
        if _shutdown_event.wait(timeout=max(interval_min * 60, 1)):
            break

    logger.info("Aasaan SQLite backup loop stopped.")


if __name__ == "__main__":
    main()
