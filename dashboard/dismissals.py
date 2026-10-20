"""Dismissed 'needs attention' items, shared by the Today page (and the archived Summary / My Hub pages) and the hub's
Telegram notifications.

There is one store: the `hub_dismissed` table, owned by scripts/myhub.py. This module is a thin
adapter so the Summary page uses it too; a dismissal anywhere hides the item everywhere it appears and
stops it from notifying. Keys are tied to the specific event (a failed run's timestamp, say), so a new
occurrence of the same kind of thing still shows up. The old state/dashboard_dismissed.json is imported
once and renamed.
"""
import sys

from .data import common

if str(common.SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(common.SCRIPTS_DIR))
import myhub as service  # noqa: E402


def _migrate_legacy() -> None:
    path = common.state_dir() / "dashboard_dismissed.json"
    data = common.read_json(path)
    if isinstance(data, dict):
        have = service.dismissed_rows(db_path=common.db_path())
        for key in data:
            if key not in have:
                service.dismiss_key(key, db_path=common.db_path())
    if path.exists():
        path.rename(path.with_suffix(".json.migrated"))


def load() -> dict:
    _migrate_legacy()
    return {k: {"at": at} for k, at in service.dismissed_rows(db_path=common.db_path()).items()}


def dismiss(key: str, text: str = "") -> None:
    _migrate_legacy()
    service.dismiss_key(key, text, db_path=common.db_path())


def restore(key: str | None = None) -> None:
    """Un-dismiss one item, or all of them when key is None."""
    service.restore(key, db_path=common.db_path())
