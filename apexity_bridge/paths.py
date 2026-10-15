"""Platform data/log directory resolution.

Windows production layout (ProgramData, writable by the install user):
    %ProgramData%\\ApexityEduBridge\\
        bridge.db
        logs\\bridge.log
        backups\\

POSIX development layout:
    ~/.apexity-bridge/...

Everything here is side-effect free until ``ensure_dir`` is called.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "ApexityEduBridge"


def is_windows() -> bool:
    return sys.platform.startswith("win")


def default_data_dir() -> Path:
    """Base directory that holds the DB, backups, and (by default) logs."""
    override = os.environ.get("BRIDGE_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if is_windows():
        program_data = os.environ.get("ProgramData", r"C:\ProgramData")
        return Path(program_data) / APP_DIR_NAME
    return Path.home() / ".apexity-bridge"


def default_db_path() -> Path:
    override = os.environ.get("BRIDGE_DB_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return default_data_dir() / "bridge.db"


def default_log_dir() -> Path:
    override = os.environ.get("LOG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return default_data_dir() / "logs"


def backup_dir() -> Path:
    return default_data_dir() / "backups"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
