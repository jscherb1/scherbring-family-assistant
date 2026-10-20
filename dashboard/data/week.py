"""Week page reader: the next 7 days in one view, one column per day.

Pure composition of existing readers, no rules of its own:
- calendar events, tasks and the day's dinner event from the hub snapshot (scripts/myhub.py, via data/myhub.py),
- planned dinners from the meal-planner's logged plans (data/activity.py),
- maintenance due dates from the hub's home card, planned workouts (data/shopping_fitness.py),
- birthdays (data/family.py).
"""
from datetime import date, timedelta

from . import activity, common, family, myhub, shopping_fitness

DAYS = 7
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def get_week():
    today = common.today()
    days = [today + timedelta(days=i) for i in range(DAYS)]
    by_day = {d.isoformat(): {"date": d.isoformat(), "label": "Today" if d == today else d.strftime("%a"),
                              "sub": d.strftime("%b %-d"), "events": [], "dinner": None, "tasks": [],
                              "chores": [], "workouts": [], "birthdays": []} for d in days}
    end = days[-1].isoformat()

    def put(day, key, item):
        if day in by_day:
            by_day[day][key].append(item)

    hub = myhub.get_hub()
    cards = hub.get("cards", {}) if "error" not in hub else {}
    s = cards.get("schedule") or {}
    for e in (s.get("today") or []) + (s.get("upcoming") or []):
        put(e["day"], "events", {"time": e.get("start_time"), "title": e["title"]})
    if s.get("dinner"):
        by_day[today.isoformat()]["dinner"] = s["dinner"]["title"]
    for e in s.get("next_dinners") or []:
        if e["day"] in by_day and not by_day[e["day"]]["dinner"]:
            by_day[e["day"]]["dinner"] = e["title"]
    tasks = s.get("tasks") or {}
    for t in (tasks.get("today") or []) + (tasks.get("soon") or []):
        put(t["day"], "tasks", {"title": t["title"]})

    meals = activity.get_meal_insights()
    for m in meals.get("menu", []) if "error" not in meals else []:
        if m["date"] in by_day and not by_day[m["date"]]["dinner"]:
            by_day[m["date"]]["dinner"] = m["title"]

    home = cards.get("home") or {}
    for r in home.get("soon") or []:
        put(r["due"], "chores", {"title": r["name"], "href": "/home"})

    fit = shopping_fitness.get_fitness()
    if "error" not in fit and fit.get("is_current_week"):
        monday = date.fromisoformat(fit["week_start"])
        for w in fit.get("week", []):
            if w["day_of_week"] in WEEKDAYS:
                d = (monday + timedelta(days=WEEKDAYS.index(w["day_of_week"]))).isoformat()
                put(d, "workouts", {"title": w.get("workout_name") or w.get("subtype") or w["workout_type"],
                                    "time": w.get("planned_time")})

    prof = family.get_profile()
    for p in prof.get("upcoming", []) if "error" not in prof else []:
        if p["next_birthday"] and p["next_birthday"] <= end:
            put(p["next_birthday"], "birthdays", {"name": p["name"], "turning": p.get("turning")})

    overdue = {"tasks": [t["title"] for t in tasks.get("overdue") or []],
               "maintenance": [r["name"] for r in home.get("overdue") or []]}
    return {"days": list(by_day.values()), "overdue": overdue,
            "synced": (s.get("synced") or {}), "start": today.isoformat(), "end": end}
