"""Mood: today's day rating, recent trend, and the nightly prompt."""
from datetime import timedelta

import dayrating

PROVIDER = {"name": "mood", "title": "Mood"}


def collect(conn, today):
    db = conn.execute("PRAGMA database_list").fetchone()[2]
    st = dayrating.status(db_path=db, today=today)
    ratings = dayrating.list_ratings(days=30, db_path=db, today=today)
    by = {r["date"]: r["score"] for r in ratings}
    trend = [{"day": (today - timedelta(days=29 - i)).isoformat(),
              "score": by.get((today - timedelta(days=29 - i)).isoformat())} for i in range(30)]
    attention = []
    if not st["rated_today"]:
        level = "warn" if datetime_hour() >= 20 else "info"
        attention.append({"key": f"mood:unrated:{today}", "href": "/checkin", "level": level,
                          "text": "Today isn't rated yet"})
    if st["yesterday_unrated"] and st["total_ratings"]:
        attention.append({"key": f"mood:unrated-yday:{today}", "href": "/checkin?date=yesterday", "level": "info",
                          "text": "Yesterday isn't rated"})
    return {"card": {"today": st["today"], "avg_7": st["avg_7"], "avg_30": st["avg_30"],
                     "streak": st["logging_streak"], "trend": trend, "total": st["total_ratings"]},
            "attention": attention}


def datetime_hour() -> int:
    from datetime import datetime
    return datetime.now().hour
