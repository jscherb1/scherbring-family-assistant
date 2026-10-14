"""Home & yard: overdue and soon-due maintenance, active lawn issues."""
from datetime import date, timedelta

PROVIDER = {"name": "home", "title": "Home & yard"}
SOON_DAYS = 14


def _d(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def collect(conn, today):
    items = conn.execute("""
        SELECT i.id, i.name, i.interval_days, i.created_at,
               (SELECT MAX(completed_date) FROM home_maintenance_completions c WHERE c.item_id=i.id) AS last_done
          FROM home_maintenance_items i WHERE i.active=1""").fetchall()
    overdue, soon = [], []
    for it in items:
        base = _d(it["last_done"]) or _d(it["created_at"]) or today
        due = base + timedelta(days=it["interval_days"])
        row = {"id": it["id"], "name": it["name"], "due": due.isoformat(), "days": (due - today).days}
        if row["days"] < 0:
            overdue.append(row)
        elif row["days"] <= SOON_DAYS:
            soon.append(row)
    issues = [dict(r) for r in conn.execute(
        "SELECT * FROM lawn_garden_issues WHERE status='active' ORDER BY created_at DESC LIMIT 5").fetchall()] \
        if _has(conn, "lawn_garden_issues") else []
    attention = [{"key": f"home:{r['id']}:{r['due']}", "href": "/home", "level": "warn",
                  "text": f"{r['name']} is overdue ({-r['days']}d)"} for r in overdue]
    attention += [{"key": f"home-soon:{r['id']}:{r['due']}", "href": "/home", "level": "info",
                   "text": f"{r['name']} due in {r['days']}d"} for r in soon if r["days"] <= 3]
    return {"card": {"overdue": overdue, "soon": sorted(soon, key=lambda r: r["days"]), "tracked": len(items),
                     "lawn_issues": len(issues)}, "attention": attention}


def _has(conn, table):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone())
