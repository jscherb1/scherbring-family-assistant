"""Home Assistant reader for the dashboard.

No logic of its own: the board, health rules and write allow-list all live in scripts/ha.py, the
same code the `ha-overview` subagent's CLI runs. Errors (HA unreachable) become {"error": ...}.
"""
import sys

from . import common

sys.path.insert(0, str(common.SCRIPTS_DIR))
import ha as service  # noqa: E402


def _safe(fn):
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except service.HAError as exc:
            return {"error": exc.message}
    wrapper.__name__ = fn.__name__
    return wrapper


@_safe
def get_board():
    return service.board()


@_safe
def get_health():
    return service.health(db_path=common.db_path())
