"""Hy-Vee and Fitness readers (read-only). Never touches the Hy-Vee/COROS login sessions."""
from datetime import timedelta

from . import common

AUTO_THRESHOLD = 0.75  # matches scripts/hyvee_store.py


@common.guarded
def get_hyvee():
    conn = common.connect_ro()
    try:
        top = common.rows(conn, """
            SELECT product_name, COUNT(*) AS times, MAX(order_date) AS last_order
              FROM hyvee_purchase_history GROUP BY product_name
             ORDER BY times DESC, last_order DESC LIMIT 15""")
        hist = common.rows(conn, """
            SELECT COUNT(*) AS lines, COUNT(DISTINCT purchase_id) AS orders,
                   MIN(order_date) AS first_order, MAX(order_date) AS last_order,
                   MAX(synced_at) AS last_sync FROM hyvee_purchase_history""")[0]
        prefs = common.rows(conn, "SELECT item, product_name, size, confidence, source, "
                                  "times_confirmed, times_rejected FROM hyvee_item_prefs "
                                  "ORDER BY confidence DESC, item")
        feedback = common.rows(conn, "SELECT action, COUNT(*) AS n FROM hyvee_feedback_log GROUP BY action")
        runs = common.rows(conn, "SELECT ts, items_json, resolved_json, cart_verified, summary "
                                 "FROM hyvee_cart_runs ORDER BY ts DESC LIMIT 10")
    finally:
        conn.close()

    for r in runs:
        r["item_count"] = len(common.load_json(r.pop("items_json"), []))
        resolved = common.load_json(r.pop("resolved_json"), [])
        if isinstance(resolved, dict):
            resolved = resolved.get("resolved", list(resolved.values()))
        decisions = [e.get("decision") for e in resolved if isinstance(e, dict)]
        r["auto"] = decisions.count("auto")
        r["flagged"] = decisions.count("flag") + decisions.count("search")
    fb = {f["action"]: f["n"] for f in feedback}
    judged = fb.get("accepted", 0) + fb.get("rejected", 0) + fb.get("substituted", 0)
    return {
        "history": hist, "top_items": top,
        "prefs": prefs, "auto_prefs": sum(1 for p in prefs if p["confidence"] >= AUTO_THRESHOLD),
        "low_prefs": sum(1 for p in prefs if p["confidence"] < AUTO_THRESHOLD),
        "threshold": AUTO_THRESHOLD, "feedback": fb,
        "acceptance_rate": round(100 * fb.get("accepted", 0) / judged) if judged else None,
        "runs": runs,
    }


@common.guarded
def get_fitness():
    today = common.today()
    monday = today - timedelta(days=today.weekday())
    conn = common.connect_ro()
    try:
        week = common.rows(conn, """
            SELECT p.day_of_week, p.workout_type, p.subtype, p.planned_time, p.coros_status,
                   p.is_custom, p.week_start_date, l.name AS workout_name
              FROM fitness_weekly_plans p LEFT JOIN fitness_workout_library l ON l.id = p.matched_library_id
             WHERE p.week_start_date = ? ORDER BY p.planned_time, p.created_at""", (monday.isoformat(),))
        latest = common.rows(conn, "SELECT MAX(week_start_date) AS w FROM fitness_weekly_plans")[0]["w"]
        weeks = common.rows(conn, """
            SELECT week_start_date, COUNT(*) AS planned,
                   SUM(is_custom) AS custom,
                   SUM(coros_status IN ('failed','manual_needed')) AS needs_attention
              FROM fitness_weekly_plans GROUP BY week_start_date ORDER BY week_start_date DESC LIMIT 8""")
        library = common.rows(conn, "SELECT workout_type, COUNT(*) AS n, MAX(last_synced_at) AS synced "
                                    "FROM fitness_workout_library GROUP BY workout_type ORDER BY n DESC")
    finally:
        conn.close()
    shown_week = monday.isoformat()
    if not week and latest:  # nothing planned this week: show the most recent plan instead
        shown_week = latest
        conn = common.connect_ro()
        try:
            week = common.rows(conn, """
                SELECT p.day_of_week, p.workout_type, p.subtype, p.planned_time, p.coros_status,
                       p.is_custom, p.week_start_date, l.name AS workout_name
                  FROM fitness_weekly_plans p LEFT JOIN fitness_workout_library l ON l.id = p.matched_library_id
                 WHERE p.week_start_date = ? ORDER BY p.planned_time, p.created_at""", (latest,))
        finally:
            conn.close()
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    week.sort(key=lambda w: (days.index(w["day_of_week"]) if w["day_of_week"] in days else 9,
                             w["planned_time"] or ""))
    return {
        "week_start": shown_week, "is_current_week": shown_week == monday.isoformat(),
        "week": week, "weeks": weeks, "library": library,
        "library_total": sum(l["n"] for l in library),
        "last_sync": max((l["synced"] for l in library), default=None),
        "needs_attention": sum(1 for w in week if w["coros_status"] in ("failed", "manual_needed")),
    }
