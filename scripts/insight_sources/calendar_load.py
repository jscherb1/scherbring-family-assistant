"""Source: how busy the calendar was per day (events, hours booked, work shift, travel, kid activity).

Reads the calendar_load table, filled by `dayrating_store.py calendar sync` (the dayrating agent reads
Google Calendar through its connector). Days never synced have no value, so they're skipped.
"""
SOURCE = {"name": "calendar_load", "kind": "agent",
          "description": "Calendar events, hours booked, Caroline's work shift, travel and kid activities per day"}

COLUMNS = {"events": "events", "hours_booked": "hours_booked", "caroline_work": "caroline_work",
           "travel": "travel", "kid_activity": "kid_activity"}


def collect(conn, start, end, today):
    rows = conn.execute("SELECT * FROM calendar_load WHERE day >= ? AND day <= ?",
                        (start.isoformat(), end.isoformat())).fetchall()
    return {f"calendar:{name}": {r["day"]: float(r[col]) for r in rows} for name, col in COLUMNS.items()} if rows else {}
