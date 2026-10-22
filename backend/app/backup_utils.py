"""Shared helpers and configuration for SQLite backup and restore."""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional, Union

logger = logging.getLogger("aasaan.backup")

# Root of the repo (backend/app/backup_utils.py -> backend -> repo root)
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"

BACKUP_FILENAME_PATTERN = re.compile(r"^aasaan-\d{4}-\d{2}-\d{2}-\d{4}(\d{2})?\.db$")


def get_backup_dir(override: Optional[Union[str, Path]] = None) -> Path:
    """Resolve backup directory from override, AASAAN_BACKUP_DIR env var, or default."""
    if override is not None:
        return Path(override).resolve()
    env_val = os.environ.get("AASAAN_BACKUP_DIR", "").strip()
    if env_val:
        return Path(env_val).resolve()
    return DEFAULT_BACKUP_DIR.resolve()


def check_dir_writable(directory: Union[Path, str], name: str = "Backup") -> Path:
    """Ensure directory exists and is writable by non-root process (UID 10001)."""
    dir_p = Path(directory).resolve()
    try:
        dir_p.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise RuntimeError(f"{name} directory {dir_p} cannot be created: {exc}") from exc

    probe = dir_p / f".probe_{os.getpid()}_{time.time_ns()}"
    try:
        probe.write_text("probe", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except Exception as exc:
        raise PermissionError(f"{name} directory {dir_p} is not writable: {exc}") from exc

    return dir_p


def verify_integrity(db_path: Path) -> tuple[bool, str]:
    """Run PRAGMA integrity_check on SQLite database file.

    Returns (True, 'ok') if healthy, or (False, reason) if corrupted.
    """
    if not db_path.exists() or not db_path.is_file():
        return False, "File does not exist or is not a regular file"

    try:
        # Open in read-only URI mode to prevent any side-effect modifications
        conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True, timeout=10)
        try:
            cur = conn.cursor()
            rows = cur.execute("PRAGMA integrity_check;").fetchall()
            if rows and len(rows) == 1 and rows[0][0] == "ok":
                return True, "ok"
            reasons = "; ".join(str(r[0]) for r in rows) if rows else "no output"
            return False, reasons
        finally:
            conn.close()
    except Exception as exc:
        return False, str(exc)


def remove_wal_and_shm(db_path: Union[Path, str]) -> None:
    """Remove stale SQLite -wal and -shm files alongside target database."""
    base = str(db_path)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{base}{suffix}")
        try:
            if sidecar.exists():
                sidecar.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Could not remove stale %s: %s", sidecar, exc)


def list_backups(backup_dir: Path) -> list[Path]:
    """List validly named aasaan-*.db backups in backup_dir sorted newest first."""
    if not backup_dir.exists() or not backup_dir.is_dir():
        return []

    backups = [
        f for f in backup_dir.iterdir()
        if f.is_file() and BACKUP_FILENAME_PATTERN.match(f.name)
    ]
    # Sort descending by filename which starts with ISO timestamp aasaan-YYYY-MM-DD-HHMM
    backups.sort(key=lambda p: p.name, reverse=True)
    return backups
