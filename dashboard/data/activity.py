"""Meal plan/recipe insights and the cross-agent activity feed (read-only)."""
from datetime import timedelta

from . import common

STALE_DAYS = 60
FEED_LIMIT = 60
SUMMARY_MAX = 240
# Agents whose summaries can contain amounts, memory text or personal facts; the dashboard has no auth.
HIDDEN_SUMMARY_AGENTS = {"finance", "finance-reporter", "finance-advisor", "retirement",
                         "kids-memory", "profile"}


@common.guarded
def get_meal_insights():
    today = common.today()
    conn = common.connect_ro()
    try:
        plans = common.rows(conn, "SELECT created_at, detail_json FROM agent_results "
                                  "WHERE agent = 'meal-planner' AND detail_json IS NOT NULL "
                                  "ORDER BY created_at DESC LIMIT 20")
        recipes = common.rows(conn, "SELECT id, title, meal_type, protein_type, rating, last_cooked_at "
                                    "FROM recipes")
    finally:
        conn.close()

    menu = {}  # date -> meal; newest planning run wins for a given date
    for p in plans:
        detail = common.load_json(p["detail_json"], {})
        for m in detail.get("meals", []) if isinstance(detail, dict) else []:
            if not isinstance(m, dict):
                continue
            d = common.parse_date(m.get("date"))
            if d and d >= today and d.isoformat() not in menu:
                menu[d.isoformat()] = {"date": d.isoformat(), "title": m.get("title") or "(untitled)",
                                       "recipe_id": m.get("recipe_id"), "planned_on": p["created_at"]}
    known = {r["id"] for r in recipes}
    for m in menu.values():
        m["recipe_known"] = m["recipe_id"] in known

    def days_since(r):
        d = common.parse_date(r["last_cooked_at"])
        return (today - d).days if d else None

    for r in recipes:
        r["days_since_cooked"] = days_since(r)
    cutoff = timedelta(days=STALE_DAYS).days
    stale = sorted((r for r in recipes if r["days_since_cooked"] is None or r["days_since_cooked"] >= cutoff),
                   key=lambda r: (r["days_since_cooked"] is not None, -(r["days_since_cooked"] or 0), r["title"]))
    cooked = sorted((r for r in recipes if r["days_since_cooked"] is not None),
                    key=lambda r: r["days_since_cooked"])

    def count(key):
        out = {}
        for r in recipes:
            out[r[key] or "unspecified"] = out.get(r[key] or "unspecified", 0) + 1
        return sorted(out.items(), key=lambda kv: -kv[1])

    rated = [r["rating"] for r in recipes if r["rating"]]
    return {
        "menu": sorted(menu.values(), key=lambda m: m["date"]), "recipe_count": len(recipes),
        "recently_cooked": cooked[:10], "stale": stale[:15], "stale_total": len(stale),
        "stale_days": STALE_DAYS, "by_meal_type": count("meal_type"), "by_protein": count("protein_type"),
        "avg_rating": round(sum(rated) / len(rated), 1) if rated else None, "rated_count": len(rated),
    }


@common.guarded
def get_activity(agent=""):
    conn = common.connect_ro()
    try:
        per_agent = common.rows(conn, "SELECT agent, COUNT(*) AS runs, MAX(created_at) AS last_at "
                                      "FROM agent_results GROUP BY agent ORDER BY last_at DESC")
        if agent:
            feed = common.rows(conn, "SELECT id, agent, created_at, summary FROM agent_results "
                                     "WHERE agent = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                               (agent, FEED_LIMIT))
        else:
            feed = common.rows(conn, "SELECT id, agent, created_at, summary FROM agent_results "
                                     "ORDER BY created_at DESC, id DESC LIMIT ?", (FEED_LIMIT,))
    finally:
        conn.close()
    for f in feed:
        if f["agent"] in HIDDEN_SUMMARY_AGENTS:
            f["summary"] = "(summary hidden — sensitive area)"
            continue
        text = " ".join(str(f["summary"]).split())
        f["summary"] = text if len(text) <= SUMMARY_MAX else text[:SUMMARY_MAX - 1] + "…"
    return {"agents": per_agent, "feed": feed, "selected": agent,
            "total": sum(a["runs"] for a in per_agent)}
