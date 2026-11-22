"""Training reader for the dashboard.

No logic of its own: every number comes from the shared service in scripts/training.py, the same code the
`training` subagent's CLI runs. Service errors become {"error": message, "code": n} (code 2 = bad input).
"""
import functools
import sys

from . import common

sys.path.insert(0, str(common.SCRIPTS_DIR))
import training as service  # noqa: E402


def _wrap(fn):
    @common.guarded
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, db_path=common.db_path(), **kwargs)
        except service.TrainingError as exc:
            return {"error": exc.message, "code": exc.code}
    return wrapper


@_wrap
def get_sports(db_path=None):
    return service.sports_available(db_path=db_path)


@_wrap
def get_config(db_path=None):
    return service.get_config(db_path=db_path)


@_wrap
def get_zone_definitions(db_path=None):
    return service.zone_definitions(db_path=db_path)


@_wrap
def get_summary(sports=None, start=None, end=None, db_path=None):
    return service.summary(sports=sports, start=start, end=end, db_path=db_path)


@_wrap
def get_volume(metric="distance", sports=None, start=None, end=None, db_path=None):
    return service.volume(metric=metric, sports=sports, start=start, end=end, db_path=db_path)


@_wrap
def get_zone_time(bucket="total", sports=None, start=None, end=None, db_path=None):
    return service.zone_time(bucket=bucket, sports=sports, start=start, end=end, db_path=db_path)


@_wrap
def get_zone_share(bucket="week", sports=None, start=None, end=None, db_path=None):
    return service.zone_share(bucket=bucket, sports=sports, start=start, end=end, db_path=db_path)


@_wrap
def get_easy_share(bucket="week", sports=None, start=None, end=None, db_path=None):
    return service.easy_share(bucket=bucket, sports=sports, start=start, end=end, db_path=db_path)


@_wrap
def get_longest(sports=None, start=None, end=None, db_path=None):
    return service.longest_run_per_week(sports=sports, start=start, end=end, db_path=db_path)


@_wrap
def get_activities(sports=None, start=None, end=None, limit=15, db_path=None):
    return service.activities(sports=sports, start=start, end=end, limit=limit, db_path=db_path)
