"""Golf reader for the dashboard.

No logic of its own: differentials, the handicap and which rounds count all come from the
shared service in scripts/golf.py, the same code the `golf` subagent's CLI runs.
"""
import sys

from . import common

sys.path.insert(0, str(common.SCRIPTS_DIR))
import golf as service  # noqa: E402


@common.guarded
def get_rounds(limit=30):
    db = common.db_path()
    return {
        "handicap": service.get_handicap(db_path=db),
        "rounds": service.list_rounds(limit=limit, db_path=db),
        "courses": service.list_courses(db_path=db),
        "pending_sync": sum(1 for r in service.list_rounds(limit=1000, db_path=db) if not r["sheet_synced"]),
    }


@common.guarded
def get_courses():
    return {"courses": service.list_courses(db_path=common.db_path())}
