"""Dismissed 'needs attention' items on the Summary page.

Each attention item has a stable key tied to the specific event (for example a failed run's
timestamp), so a dismissal hides that event for good but a new one still shows up. Stored in
state/dashboard_dismissed.json, the dashboard's only state besides the ask box's inbox row.
"""
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

from .data import common

MAX_AGE = timedelta(days=60)


def _path():
    return common.state_dir() / "dashboard_dismissed.json"


def load() -> dict:
    data = common.read_json(_path())
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".dismissed-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def dismiss(key: str, active_keys: set[str]) -> None:
    """Dismiss one item; also forget dismissals that are stale (the condition is gone or old)."""
    data = load()
    cutoff = datetime.now(timezone.utc) - MAX_AGE
    keep = {}
    for k, v in data.items():
        at = common.parse_ts(v.get("at")) if isinstance(v, dict) else None
        if k in active_keys and at and at > cutoff:
            keep[k] = v
    keep[key] = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    _save(keep)


def restore(key: str | None = None) -> None:
    """Un-dismiss one item, or all of them when key is None."""
    data = {} if key is None else load()
    data.pop(key, None)
    _save(data)
