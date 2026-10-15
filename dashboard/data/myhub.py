"""My Hub reader for the dashboard.

No logic of its own: every card and the needs-attention list come from the shared service in
scripts/myhub.py (and its providers), the same code the `myhub` subagent's CLI runs.
"""
import sys

from . import common

sys.path.insert(0, str(common.SCRIPTS_DIR))
import myhub as service  # noqa: E402


@common.guarded
def get_hub():
    return service.get_hub(db_path=common.db_path())


def dismiss(key: str) -> None:
    service.dismiss(key, db_path=common.db_path())


def restore(key: str | None) -> None:
    service.restore(key, db_path=common.db_path())


def refresh_live() -> dict:
    return service.refresh_live(db_path=common.db_path())
