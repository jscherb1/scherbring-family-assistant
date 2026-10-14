"""Source: every habit's completions as a 0/1 metric per day (habit:<name>), plus habits-done count."""
from datetime import timedelta

SOURCE = {"name": "habits", "kind": "auto",
          "description": "Each habit as done / not done per day, and how many habits were done"}


def collect(conn, start, end, today):
    habits = conn.execute("SELECT id, name FROM habits WHERE active = 1").fetchall()
    first = start.isoformat()
    out, totals = {}, {}
    days = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    for h in habits:
        done = {r["log_date"] for r in conn.execute(
            "SELECT DISTINCT log_date FROM habit_logs WHERE habit_id = ? AND log_date >= ?", (h["id"], first))}
        out[f"habit:{h['name']}"] = {d: 1.0 if d in done else 0.0 for d in days}
        for d in done:
            totals[d] = totals.get(d, 0) + 1
    if habits:
        out["habit:total_done"] = {d: float(totals.get(d, 0)) for d in days}
    return out
