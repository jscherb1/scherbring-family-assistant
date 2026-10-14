"""Chores: what's open today per kid and what's owed."""
import chores

PROVIDER = {"name": "chores", "title": "Chores"}


def collect(conn, today):
    db = conn.execute("PRAGMA database_list").fetchone()[2]
    views = chores.all_kid_views(db_path=db, today=today)
    kids = [{"name": v["kid"]["name"], "done": v["done_count"], "total": v["total_count"],
             "owed": v["balance"], "streak": v["best_streak"],
             "open": [i["name"] for i in v["todo"] if not i["done"]]} for v in views]
    attention = []
    from datetime import datetime
    if datetime.now().hour >= 16:
        for k in kids:
            if k["open"]:
                attention.append({"key": f"chores:{k['name']}:{today}", "href": "/chores", "level": "info",
                                  "text": f"{k['name']} still has {len(k['open'])} chore(s) open"})
    return {"card": {"kids": kids}, "attention": attention}
