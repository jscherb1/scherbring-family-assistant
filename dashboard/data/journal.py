"""Journal + nightly check-in reader for the dashboard.

No logic of its own: questions, answers, the check-in's pending items and the impact numbers all come from the
shared service in scripts/journal.py, the same code the `journal` subagent's CLI runs.
"""
import sys

from . import common

sys.path.insert(0, str(common.SCRIPTS_DIR))
import journal as service  # noqa: E402


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, db_path=common.db_path(), **kwargs)
    except service.JournalError as exc:
        return {"error": exc.message, "code": exc.code}


@common.guarded
def get_checkin(day=None):
    if not day:
        day = service.checkin_date(db_path=common.db_path())
    return _call(service.pending, day)


@common.guarded
def get_history(days=28):
    return _call(service.history, days=days)


@common.guarded
def get_questions():
    return {"questions": service.list_questions(include_inactive=True, db_path=common.db_path())}


@common.guarded
def get_impact(days=90):
    return _call(service.impact, days=days)
