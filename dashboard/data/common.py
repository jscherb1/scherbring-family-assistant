"""Shared helpers for the read-only data layer (no web-framework imports)."""
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = BASE_DIR / "scripts"


def state_dir() -> Path:
    """State directory; overridable via ASSISTANT_STATE_DIR (used by tests)."""
    override = os.environ.get("ASSISTANT_STATE_DIR")
    return Path(override) if override else BASE_DIR / "state"


def db_path() -> Path:
    return state_dir() / "agent_results.db"


def parse_ts(value):
    """Parse an ISO-8601 timestamp (with or without a trailing Z) to an aware datetime."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def now():
    return datetime.now(timezone.utc)


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def connect_ro() -> sqlite3.Connection:
    """Open agent_results.db strictly read-only. Raises FileNotFoundError or
    sqlite3.OperationalError if it cannot be opened."""
    path = db_path()
    if not path.exists():
        raise FileNotFoundError(f"{path.name} not found")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn
