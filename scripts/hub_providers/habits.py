"""Habits: at-risk streaks and this week's progress."""
import habits

PROVIDER = {"name": "habits", "title": "Habits"}


def collect(conn, today):
    db = conn.execute("PRAGMA database_list").fetchone()[2]
    items = [h for h in habits.list_status(db_path=db, today=today) if h["active"]]
    goal = [h for h in items if h["has_goal"]]
    attention = []
    for h in goal:
        if h["at_risk"]:
            attention.append({"key": f"habit:{h['id']}:{h['period_start']}", "href": "/habits", "level": "warn",
                              "text": f"{h['name']} streak ({h['current_streak']}) is at risk"})
        elif h["frequency"] == "daily" and h["period_status"] != "met":
            attention.append({"key": f"habit-today:{h['id']}:{today}", "href": "/habits", "level": "info",
                              "text": f"{h['name']} still open today"})
    return {"card": {
        "goal": [{"name": h["name"], "streak": h["current_streak"], "done": h["done_this_period"],
                  "target": h["target_count"], "frequency": h["frequency"], "status": h["period_status"]}
                 for h in goal],
        "tracking": [{"name": h["name"], "week": h.get("count_this_week"), "last30": h.get("count_last_30_days")}
                     for h in items if not h["has_goal"]],
        "week_strip": _strip(conn, today),
    }, "attention": attention}


def _strip(conn, today):
    """Habits done per day for the last 28 days (heat strip)."""
    from datetime import timedelta
    since = (today - timedelta(days=27)).isoformat()
    rows = conn.execute("SELECT log_date, COUNT(DISTINCT habit_id) n FROM habit_logs WHERE log_date>=? GROUP BY log_date",
                        (since,)).fetchall()
    by = {r["log_date"]: r["n"] for r in rows}
    return [{"day": (today - timedelta(days=27 - i)).isoformat(),
             "n": by.get((today - timedelta(days=27 - i)).isoformat(), 0)} for i in range(28)]
