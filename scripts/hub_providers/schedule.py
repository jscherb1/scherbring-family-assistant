"""Schedule: today's events, upcoming days, tasks (from the agent-synced snapshot), dinner."""
import hashlib
from datetime import datetime, timedelta

from myhub import STALE_HOURS

PROVIDER = {"name": "schedule", "title": "Schedule"}


def _items(conn, kind, start, end):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM hub_items WHERE kind=? AND day BETWEEN ? AND ? "
        "ORDER BY day, COALESCE(start_time,'00:00'), title", (kind, start, end)).fetchall()]


def _tkey(prefix, x):
    """Stable across refreshes: the snapshot gives tasks new row ids every time, so key on the title."""
    return f"{prefix}:" + hashlib.sha1(x["title"].strip().lower().encode()).hexdigest()[:12]


def collect(conn, today):
    t = today.isoformat()
    week_end = (today + timedelta(days=7)).isoformat()
    events = _items(conn, "event", t, week_end)
    meal = [e for e in events if e["day"] == t and (e["source"] or "").lower().startswith("meal")]
    cal_today = [e for e in events if e["day"] == t and e not in meal]
    upcoming = [e for e in events if e["day"] > t and e not in
                [x for x in events if (x["source"] or "").lower().startswith("meal")]]
    tasks = [dict(r) for r in conn.execute(
        "SELECT * FROM hub_items WHERE kind='task' ORDER BY day, COALESCE(priority,4), title").fetchall()]
    overdue = [x for x in tasks if x["day"] < t]
    due_today = [x for x in tasks if x["day"] == t]
    soon = [x for x in tasks if t < x["day"] <= week_end]
    next_meals = [e for e in events if e["day"] > t and (e["source"] or "").lower().startswith("meal")]
    attention = []
    for x in overdue:
        attention.append({"key": _tkey("task", x), "href": "/me", "level": "urgent", "group": "Tasks",
                          "text": f"Overdue: {x['title']} (due {x['day']})"})
    for x in due_today:
        attention.append({"key": _tkey("task-today", x), "href": "/me",
                          "level": "warn" if (x["priority"] or 4) <= 2 else "info", "group": "Tasks", "text": f"Due today: {x['title']}"})
    synced = {k: conn.execute("SELECT MAX(synced_at) FROM hub_items WHERE kind=?", (k,)).fetchone()[0]
              for k in ("event", "task")}
    for kind, label in (("event", "Calendar"), ("task", "Task list")):
        at = synced[kind]
        stale = at is None or datetime.now() - datetime.fromisoformat(at) > timedelta(hours=STALE_HOURS)
        if stale:
            attention.append({"key": f"hub-stale:{kind}:{today}", "href": "/me", "level": "info",
                              "text": f"{label} on this page is {'not synced yet' if at is None else 'out of date'}"})
    return {"card": {"today": cal_today, "upcoming": upcoming, "dinner": meal[0] if meal else None,
                     "next_dinners": next_meals[:3],
                     "tasks": {"overdue": overdue, "today": due_today, "soon": soon[:8], "open": len(tasks)},
                     "synced": synced}, "attention": attention}
