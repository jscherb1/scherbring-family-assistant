"""Day rating + insights reader for the dashboard.

No logic of its own: ratings come from scripts/dayrating.py and the statistics / stored AI narrative
from scripts/insights.py, the same code the `dayrating` subagent's CLI runs.
"""
import sys

from . import common

sys.path.insert(0, str(common.SCRIPTS_DIR))
import dayrating as service  # noqa: E402
import insights  # noqa: E402


@common.guarded
def get_overview(days=60):
    return {"status": service.status(db_path=common.db_path()),
            "ratings": service.list_ratings(days=days, db_path=common.db_path()), "days": days}


@common.guarded
def get_insights(days=90):
    path = common.db_path()
    return {"stats": insights.analyze(days=days, db_path=path), "report": insights.latest_report(db_path=path),
            "days": days}
