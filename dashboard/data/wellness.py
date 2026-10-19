"""Wellness reader for the dashboard.

No logic of its own: readiness, timing status, baselines, trends and drivers all come from the shared
service in scripts/wellness.py, the same code the `wellness` subagent's CLI runs.
"""
import sys

from . import common

sys.path.insert(0, str(common.SCRIPTS_DIR))
import wellness as service  # noqa: E402


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, db_path=common.db_path(), **kwargs)
    except service.WellnessError as exc:
        return {"error": exc.message, "code": exc.code}


@common.guarded
def get_today():
    return _call(service.today_view)


@common.guarded
def get_trends(range_days=90):
    return _call(service.trends, range_days)


@common.guarded
def get_heatmap(metric="rating"):
    return _call(service.heatmap, metric)


@common.guarded
def get_drivers(target="rating", days=90):
    return _call(service.drivers, target, days=days)


@common.guarded
def get_scatter(x="sleep_hours", y="rating", days=90):
    return _call(service.scatter, x, y, days=days)
