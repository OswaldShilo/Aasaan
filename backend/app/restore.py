"""SQLite restore init container script for Azure Container Apps / Docker.

Runs as: python -m app.restore
Executes before the application starts.
If the database already exists or no valid backups are available, exits 0.
Exits non-zero only on unexpected system/permission errors.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import time
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
        remove_wal_and_shm,
        verify_integrity,
    )
    from .settings import get_db_path
except ImportError:
    from app.backup_utils import (
        check_dir_writable,
        get_backup_dir,
        list_backups,
        remove_wal_and_shm,
        verify_integrity,
    )
    from app.settings import get_db_path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("aasaan.restore")


def run_restore(
    db_path: Optional[Union[str, Path]] = None,
    backup_dir: Optional[Union[str, Path]] = None,
) -> int:
    """Perform pre-start database restore from newest valid backup.

    Returns:
        0 on success (including when DB already exists or no backups found).
        Raises exception / returns non-zero on unexpected permission/filesystem error.
    """
    resolved_db = get_db_path(db_path)
    if str(resolved_db) == ":memory:":
        logger.info("Database configured as in-memory (:memory:); skipping restore.")
        return 0

    db_p = Path(resolved_db).resolve()

    # 1. If AASAAN_DB already exists, do nothing and log that
    if db_p.exists():
        logger.info("Live database already exists at %s; skipping restore.", db_p)
        return 0

    # 2. Ensure parent directory of AASAAN_DB exists and is writable (UID 10001 check)
    db_parent = db_p.parent
    check_dir_writable(db_parent, name="Database parent")

    # 3. Check backup directory
    resolved_backup_dir = get_backup_dir(backup_dir)
    if not resolved_backup_dir.exists():
        logger.info("Backup directory %s does not exist; starting with clean database.", resolved_backup_dir)
        return 0

    # 4. List backups in AASAAN_BACKUP_DIR, newest first
    backups = list_backups(resolved_backup_dir)
    if not backups:
        logger.info("No backups found in %s; starting clean.", resolved_backup_dir)
        return 0

    # 5. Find the first backup that passes PRAGMA integrity_check
    chosen_backup: Optional[Path] = None
    for candidate in backups:
        ok, reason = verify_integrity(candidate)
        if ok:
            chosen_backup = candidate
            logger.info("Selected newest valid backup: %s", candidate.name)
            break
        logger.warning("Backup %s is corrupt or invalid (%s); skipping.", candidate.name, reason)

    # 6. If no valid backup exists, start clean and exit 0
    if chosen_backup is None:
        logger.info("No valid backups found in %s after integrity checks; starting clean.", resolved_backup_dir)
        return 0

    # 7. Copy chosen backup to AASAAN_DB via a temp file and atomic rename
    tmp_dest = db_parent / f"{db_p.name}.restore_tmp_{os.getpid()}_{time.time_ns()}"
    tmp_dest.unlink(missing_ok=True)
    try:
        shutil.copy2(chosen_backup, tmp_dest)

        # Integrity check on the copied temp file before final placement
        ok, reason = verify_integrity(tmp_dest)
        if not ok:
            tmp_dest.unlink(missing_ok=True)
            raise RuntimeError(f"Copied backup failed integrity check before activation: {reason}")

        # Atomic rename into destination
        tmp_dest.replace(db_p)

        # Remove any stale -wal and -shm files next to it
        remove_wal_and_shm(db_p)

        size_bytes = db_p.stat().st_size
        logger.info("Successfully restored database from %s to %s (%d bytes).", chosen_backup.name, db_p, size_bytes)
        return 0
    except Exception as exc:
        tmp_dest.unlink(missing_ok=True)
        logger.error("Failed to restore backup %s: %s", chosen_backup.name, exc)
        raise


def main() -> None:
    """Init container entry point."""
    logger.info("Starting Aasaan database restore check...")
    try:
        rc = run_restore()
        sys.exit(rc)
    except Exception as exc:
        logger.error("Database restore failed: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
