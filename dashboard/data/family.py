"""People & Family readers: personal profile and kids memories (read-only).

Privacy: no dashboard auth yet, so these return summaries only — names, dates, counts
and fact *keys*. Fact values (allergies, sizes, address) and memory text are never
selected.
"""
from datetime import date, timedelta

from . import common


def _next_birthday(birthday, today):
    """(next occurrence date, age turning or None) for 'MM-DD' or 'YYYY-MM-DD'."""
    text = str(birthday or "")
    try:
        if len(text) == 5:
            month, day, year = int(text[:2]), int(text[3:]), None
        else:
            year, month, day = int(text[:4]), int(text[5:7]), int(text[8:10])
        for y in (today.year, today.year + 1):
            try:
                nxt = date(y, month, day)
            except ValueError:  # Feb 29 in a non-leap year
                nxt = date(y, 3, 1)
            if nxt >= today:
                return nxt, (y - year if year else None)
    except ValueError:
        pass
    return None, None


@common.guarded
def get_profile():
    today = common.today()
    conn = common.connect_ro()
    try:
        people = common.rows(conn, "SELECT id, name, relationship, birthday, updated_at "
                                   "FROM profile_people ORDER BY name")
        fact_keys = common.rows(conn, "SELECT person_id, key FROM profile_people_facts ORDER BY key")
        global_keys = common.rows(conn, "SELECT key, updated_at FROM profile_facts ORDER BY key")
    finally:
        conn.close()

    keys_by_person = {}
    for f in fact_keys:
        keys_by_person.setdefault(f["person_id"], []).append(f["key"])
    for p in people:
        p["fact_keys"] = keys_by_person.get(p["id"], [])
        p["next_birthday"], p["turning"] = _next_birthday(p["birthday"], today)
        p["days_until_birthday"] = (p["next_birthday"] - today).days if p["next_birthday"] else None
        p["next_birthday"] = p["next_birthday"].isoformat() if p["next_birthday"] else None

    upcoming = sorted((p for p in people if p["days_until_birthday"] is not None),
                      key=lambda p: p["days_until_birthday"])
    relationships = {}
    for p in people:
        r = p["relationship"] or "unspecified"
        relationships[r] = relationships.get(r, 0) + 1
    recent_cut = (today - timedelta(days=30)).isoformat()
    return {
        "people": people, "total": len(people), "relationships": relationships,
        "upcoming": upcoming[:10],
        "within": {d: sum(1 for p in upcoming if p["days_until_birthday"] <= d) for d in (30, 60, 90)},
        "missing_birthday": [p["name"] for p in people if not p["birthday"]],
        "no_facts": [p["name"] for p in people if not p["fact_keys"]],
        "recently_updated": [p["name"] for p in people if str(p["updated_at"])[:10] >= recent_cut],
        "global_fact_keys": global_keys,
    }


@common.guarded
def get_memories():
    today = common.today()
    conn = common.connect_ro()
    try:
        rows = common.rows(conn, "SELECT memory_date, created_at, children_json, tags_json, source, "
                                 "drive_status FROM kid_memories ORDER BY memory_date DESC")
        triggers = common.rows(conn, "SELECT kind, COUNT(*) AS n FROM kid_memory_triggers GROUP BY kind")
    finally:
        conn.close()

    per_child, per_month, tags, sources, drive = {}, {}, {}, {}, {}
    for r in rows:
        for child in common.load_json(r["children_json"], []):
            per_child[child] = per_child.get(child, 0) + 1
        month = str(r["memory_date"])[:7]
        per_month[month] = per_month.get(month, 0) + 1
        for t in common.load_json(r["tags_json"], []):
            tags[t] = tags.get(t, 0) + 1
        sources[r["source"]] = sources.get(r["source"], 0) + 1
        drive[r["drive_status"]] = drive.get(r["drive_status"], 0) + 1

    last_logged = max((str(r["created_at"])[:10] for r in rows), default=None)
    last_date = common.parse_date(last_logged)
    on_this_day = sum(1 for r in rows if str(r["memory_date"])[5:10] == today.strftime("%m-%d")
                      and str(r["memory_date"])[:4] != str(today.year))
    return {
        "total": len(rows), "per_child": per_child,
        "per_month": sorted(per_month.items(), reverse=True)[:12],
        "tags": sorted(tags.items(), key=lambda kv: -kv[1])[:20],
        "sources": sources, "drive": drive,
        "last_logged": last_logged,
        "days_since_last": (today - last_date).days if last_date else None,
        "on_this_day": on_this_day,
        "triggers": {t["kind"]: t["n"] for t in triggers},
    }
