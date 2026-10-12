"""Habits reader for the dashboard.

No logic of its own: streaks, at-risk and progress all come from the shared service in
scripts/habits.py, the same code the `habits` subagent's CLI runs.
"""
import sys

from . import common

sys.path.insert(0, str(common.SCRIPTS_DIR))
import habits as service  # noqa: E402

OWNERS = ["Justin", "Caroline", "Kids", "Family"]


@common.guarded
def get_habits(owner=None, include_paused=True):
    items = service.list_status(owner=owner or None, include_inactive=include_paused, db_path=common.db_path())
    active = [h for h in items if h["active"]]
    goal = [h for h in active if h["has_goal"]]
    return {
        "habits": items,
        "owners": OWNERS,
        "owner": owner or "",
        "counts": {
            "active": len(active),
            "at_risk": sum(1 for h in active if h["at_risk"]),
            "met": sum(1 for h in goal if h["period_status"] == "met"),
            "best_streak": max((h["current_streak"] for h in goal), default=0),
        },
    }


@common.guarded
def get_one(habit_id):
    try:
        return service.get_status(habit_id, db_path=common.db_path())
    except service.HabitError as exc:
        return {"error": exc.message}
