"""Source: what kind of day it is (weekday / weekend / each weekday) as 0/1 metrics."""
from datetime import timedelta

SOURCE = {"name": "calendar_day", "kind": "auto", "description": "Day of week and weekend flag"}


def collect(conn, start, end, today):
    out = {"day:weekend": {}}
    names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    for n in names:
        out[f"day:{n}"] = {}
    for i in range((end - start).days + 1):
        d = start + timedelta(days=i)
        iso = d.isoformat()
        out["day:weekend"][iso] = 1.0 if d.weekday() >= 5 else 0.0
        for k, n in enumerate(names):
            out[f"day:{n}"][iso] = 1.0 if d.weekday() == k else 0.0
    return out
