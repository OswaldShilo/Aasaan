"""Aasaan configuration and storage path settings.

Provides centralized resolution of database and uploads storage paths with
startup validation, directory creation, and non-root writability checks.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional, Union

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = ROOT / "data" / "aasaan.db"
DEFAULT_UPLOADS_DIR = ROOT / "data" / "uploads"


def get_db_path(override: Optional[Union[str, Path]] = None) -> Union[Path, str]:
    """Resolve SQLite database path from override, AASAAN_DB env var, or default.

    Special ':memory:' string is preserved.
    """
    if override is not None:
        return override if str(override) == ":memory:" else Path(override).resolve()
    env_val = os.environ.get("AASAAN_DB", "").strip()
    if env_val:
        return env_val if env_val == ":memory:" else Path(env_val).resolve()
    return DEFAULT_DB_PATH.resolve()


def get_uploads_dir(override: Optional[Union[str, Path]] = None) -> Path:
    """Resolve uploads directory from override, AASAAN_UPLOADS env var, or default."""
    if override is not None:
        return Path(override).resolve()
    env_val = os.environ.get("AASAAN_UPLOADS", "").strip()
    if env_val:
        return Path(env_val).resolve()
    return DEFAULT_UPLOADS_DIR.resolve()


def validate_and_prepare_paths(
    db_path: Union[Path, str],
    uploads_dir: Union[Path, str],
) -> None:
    """Validate and prepare database parent directory and uploads directory on startup.

    Creates directories if they do not exist, tests write permissions for
    unprivileged container execution (e.g. uid 10001), and fails fast with
    clear errors if paths are invalid or unwritable.
    """
    # 1. Prepare and validate SQLite DB parent directory (unless :memory:)
    if str(db_path) != ":memory:":
        db_p = Path(db_path)
        db_parent = db_p.parent
        try:
            db_parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            raise RuntimeError(f"Database directory {db_parent} cannot be created: {exc}") from exc

        # Probe write access on the DB directory (required for SQLite WAL/SHM and locks)
        probe = db_parent / f".db_write_probe_{os.getpid()}_{time.time_ns()}"
        try:
            probe.write_text("probe", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except Exception as exc:
            raise PermissionError(f"Database directory {db_parent} is not writable: {exc}") from exc

        # If the database file already exists, verify file writability
        if db_p.exists():
            try:
                with open(db_p, "a+"):
                    pass
            except Exception as exc:
                raise PermissionError(f"Database file {db_p} is not writable: {exc}") from exc

    # 2. Prepare and validate uploads directory
    up_p = Path(uploads_dir)
    try:
        up_p.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise RuntimeError(f"Uploads directory {up_p} cannot be created: {exc}") from exc

    # Probe write access in uploads directory
    probe_up = up_p / f".upload_write_probe_{os.getpid()}_{time.time_ns()}"
    try:
        probe_up.write_text("probe", encoding="utf-8")
        probe_up.unlink(missing_ok=True)
    except Exception as exc:
        raise PermissionError(f"Uploads directory {up_p} is not writable: {exc}") from exc
