"""Shared helpers for hub providers."""
from datetime import date, timedelta


def series(conn, metric: str, today: date, days: int = 30) -> dict[str, float]:
    since = (today - timedelta(days=days - 1)).isoformat()
    rows = conn.execute("SELECT day, value FROM insight_daily WHERE metric=? AND day>=? AND day<=? ORDER BY day",
                        (metric, since, today.isoformat())).fetchall()
    return {r["day"]: r["value"] for r in rows}


def latest(conn, metric: str, today: date, within: int = 7):
    """(day, value) of the newest reading in the last `within` days, or None."""
    s = series(conn, metric, today, within)
    if not s:
        return None
    d = max(s)
    return d, s[d]


def count_days(conn, metric: str) -> int:
    return conn.execute("SELECT COUNT(DISTINCT day) FROM insight_daily WHERE metric=?", (metric,)).fetchone()[0]
