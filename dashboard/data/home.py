"""Home & Yard readers: home maintenance, lawn & garden, weather reminders (read-only)."""
from datetime import timedelta

from . import common

DUE_SOON_DAYS = 14


@common.guarded
def get_maintenance():
    """Items bucketed overdue / due soon / ok with days until due. Mirrors
    home_maintenance_store.cmd_due_list: last done = latest completion, else created_at."""
    today = common.today()
    conn = common.connect_ro()
    try:
        items = common.rows(conn, """
            SELECT i.id, i.name, i.category, i.interval_days, i.active, i.notes, i.created_at,
                   (SELECT MAX(completed_date) FROM home_maintenance_completions c
                     WHERE c.item_id = i.id) AS last_done,
                   (SELECT COUNT(*) FROM home_maintenance_completions c
                     WHERE c.item_id = i.id) AS completions
              FROM home_maintenance_items i ORDER BY i.name""")
        recent = common.rows(conn, """
            SELECT c.completed_date, c.notes, i.name FROM home_maintenance_completions c
              JOIN home_maintenance_items i ON i.id = c.item_id
             ORDER BY c.completed_date DESC, c.created_at DESC LIMIT 15""")
    finally:
        conn.close()

    for it in items:
        base = common.parse_date(it["last_done"]) or common.parse_date(it["created_at"]) or today
        it["never_done"] = it["last_done"] is None
        it["due_date"] = (base + timedelta(days=it["interval_days"])).isoformat()
        it["days_until_due"] = (base + timedelta(days=it["interval_days"]) - today).days
        if not it["active"]:
            it["bucket"] = "paused"
        elif it["days_until_due"] < 0:
            it["bucket"] = "overdue"
        elif it["days_until_due"] <= DUE_SOON_DAYS:
            it["bucket"] = "soon"
        else:
            it["bucket"] = "ok"
    order = {"overdue": 0, "soon": 1, "ok": 2, "paused": 3}
    items.sort(key=lambda i: (order[i["bucket"]], i["days_until_due"]))
    counts = {b: sum(1 for i in items if i["bucket"] == b) for b in order}
    categories = {}
    for it in items:
        categories[it["category"]] = categories.get(it["category"], 0) + 1
    return {"items": items, "counts": counts, "categories": categories, "recent": recent,
            "due_soon_days": DUE_SOON_DAYS}


@common.guarded
def get_lawn():
    today = common.today()
    conn = common.connect_ro()
    try:
        program = common.rows(conn, "SELECT * FROM lawn_garden_program ORDER BY round_number")
        treatments = common.rows(
            conn, "SELECT * FROM lawn_garden_treatments ORDER BY treatment_date DESC, created_at DESC")
        issues = common.rows(
            conn, "SELECT * FROM lawn_garden_issues ORDER BY status, first_noted DESC")
        products = common.rows(conn, "SELECT * FROM lawn_garden_products ORDER BY category, name")
        plants = common.rows(conn, "SELECT * FROM lawn_garden_plants ORDER BY location, name")
    finally:
        conn.close()

    this_year = [t for t in treatments if str(t["treatment_date"]).startswith(str(today.year))]
    done_rounds = {t["round_number"] for t in this_year if t["round_number"]}
    rounds = []
    next_due = None
    for p in program:
        applied = [t for t in this_year if t["round_number"] == p["round_number"]]
        if applied:
            status = "done"
        elif p["timing_month"] < today.month:
            status = "missed"
        elif p["timing_month"] == today.month:
            status = "due"
        else:
            status = "upcoming"
        r = {**p, "status": status, "applied_on": applied[0]["treatment_date"] if applied else None}
        rounds.append(r)
        if next_due is None and status in ("due", "missed", "upcoming"):
            next_due = r
    for t in treatments:
        t["targets"] = common.load_json(t["target_weeds_json"], [])
    for i in issues:
        first = common.parse_date(i["first_noted"])
        end = common.parse_date(i["resolved_date"]) or today
        i["age_days"] = (end - first).days if first else None
    return {
        "year": today.year, "rounds": rounds, "rounds_done": len(done_rounds),
        "rounds_total": len(program), "next_round": next_due,
        "treatments": treatments[:20], "treatment_count": len(treatments),
        "issues": issues, "active_issues": sum(1 for i in issues if i["status"] == "active"),
        "products": products, "out_of_stock": sum(1 for p in products if not p["in_stock"]),
        "plants": plants,
    }


# Config keys that locate the home; shown on no page.
_HIDDEN_WEATHER_KEYS = {"latitude", "longitude"}


@common.guarded
def get_weather():
    today = common.today()
    conn = common.connect_ro()
    try:
        config = common.rows(conn, "SELECT key, value FROM weather_config ORDER BY key")
        alerts = common.rows(conn, "SELECT * FROM weather_alerts ORDER BY target_date DESC, created_at DESC")
    finally:
        conn.close()
    config = [c for c in config if c["key"] not in _HIDDEN_WEATHER_KEYS]

    def within(days):
        cutoff = today - timedelta(days=days)
        return [a for a in alerts if (common.parse_date(a["target_date"]) or cutoff) >= cutoff]

    by_type = {}
    for a in within(30):
        by_type[a["alert_type"]] = by_type.get(a["alert_type"], 0) + 1
    upcoming = [a for a in alerts if (common.parse_date(a["target_date"]) or today) >= today]
    return {
        "config": config, "alerts": alerts[:30], "total": len(alerts),
        "last_7d": len(within(7)), "last_30d": len(within(30)), "by_type_30d": by_type,
        "upcoming": sorted(upcoming, key=lambda a: a["target_date"]),
        "task_created": sum(1 for a in alerts if a["todoist_task_created"]),
    }
