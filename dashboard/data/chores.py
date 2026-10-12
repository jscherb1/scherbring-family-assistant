"""Chore chart reader for the dashboard (adult /chores section and the kid /kids page).

No logic of its own: schedules, streaks, money, bonuses and badges all come from the shared
service in scripts/chores.py, the same code the `chores` subagent's CLI runs.
"""
import sys
from datetime import date

from . import common

sys.path.insert(0, str(common.SCRIPTS_DIR))
import chores as service  # noqa: E402

WEEKDAYS = service.WEEKDAYS

# Rotated by date on the kid page (static, deterministic: same all day, new tomorrow).
DAILY_MESSAGES = [
    "Chore power activate! ⚡", "You're doing great today! 🌈", "Every chore makes you stronger 💪",
    "Why did the cookie go to the doctor? It felt crummy! 🍪", "Teamwork makes the dream work! 🤝",
    "Fun fact: a group of flamingos is called a flamboyance! 🦩", "Let's crush it today! 🚀",
    "What do you call a sleeping bull? A bulldozer! 🐂", "Small jobs, big smiles 😄",
    "Fun fact: octopuses have three hearts! 🐙", "You're a chore champion in the making! 🏆",
    "What's a cat's favorite color? Purr-ple! 🐱", "Ready, set, sparkle! ✨",
    "Fun fact: bananas are berries! 🍌",
]


def daily_message(day: date | None = None) -> str:
    day = day or date.today()
    return DAILY_MESSAGES[day.toordinal() % len(DAILY_MESSAGES)]


@common.guarded
def get_overview():
    views = service.all_kid_views(db_path=common.db_path())
    return {
        "kids": views,
        "summary": {s["kid_id"]: s for s in service.week_summary(db_path=common.db_path())},
        "owed_total_cents": sum(v["balance_cents"] for v in views),
        "owed_total": service.money(sum(v["balance_cents"] for v in views)),
        "done_count": sum(v["done_count"] for v in views),
        "total_count": sum(v["total_count"] for v in views),
    }


@common.guarded
def get_manage():
    db = common.db_path()
    return {"chores": service.list_chores(include_inactive=True, db_path=db),
            "kids": service.list_kids(include_inactive=True, db_path=db), "weekdays": WEEKDAYS}


@common.guarded
def get_rewards_page():
    db = common.db_path()
    return {"rewards": service.list_rewards(include_inactive=True, db_path=db),
            "kids": service.all_kid_views(db_path=db), "config": service.get_config(db_path=db),
            "config_defaults": service.CONFIG_DEFAULTS}


@common.guarded
def get_ledger_page():
    db = common.db_path()
    kids = service.list_kids(db_path=db)
    return {"kids": [{**k, "balance_cents": service.balance(k["id"], db_path=db),
                      "balance": service.money(service.balance(k["id"], db_path=db)),
                      "ledger": service.ledger(k["id"], limit=25, db_path=db)} for k in kids],
            "chores": service.list_chores(db_path=db), "rewards": service.list_rewards(db_path=db)}


@common.guarded
def get_kid(kid_id):
    try:
        return service.kid_view(kid_id, db_path=common.db_path())
    except service.ChoreError as exc:
        return {"error": exc.message}


@common.guarded
def get_kids_page():
    return {"kids": service.all_kid_views(db_path=common.db_path()), "message": daily_message()}
