"""Rivian (R2) reader for the dashboard.

No logic of its own: status, trends, labels and health rules all live in scripts/rivian.py, the same code the
`ha-overview` subagent's CLI (scripts/rivian_store.py) runs. Errors (HA unreachable, no car) become {"error": ...}.
"""
import sys

from . import common

sys.path.insert(0, str(common.SCRIPTS_DIR))
import rivian as service  # noqa: E402


def _safe(fn):
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except service.RivianError as exc:
            return {"error": exc.message, "code": exc.code}
    wrapper.__name__ = fn.__name__
    return wrapper


@_safe
def get_status():
    return service.status()


@_safe
def get_trends(days: int = 30):
    return service.trends(days)


@_safe
def get_trips(days: int = 30):
    return service.trips(days, db_path=common.db_path())


@_safe
def get_brief():
    return service.brief(db_path=common.db_path())
