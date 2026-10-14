"""Per-day calendar load (shared service for the calendar_load insight source).

Google Calendar is only reachable through the agent's connector, so the dayrating agent reads events
and hands them here as JSON; this module owns every rule (which calendars count, overlaps, flags) and
the storage. Days in the synced range with no events are stored as zero so "empty day" is a real value.

Event JSON: {"title", "calendar", "start", "end", "all_day"}; start/end are ISO date or datetime.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from insights import InsightError, connect as _connect

EXCLUDED_CALENDARS = ("meal planning", "holiday", "running")  # planned meals/workouts aren't load
CAROLINE_WORK = "caroline work"
TRAVEL_WORDS = ("flight", "airport", "travel", "trip", "drive to", "road trip", "depart")
KID_WORDS = ("dance", "practice", "lesson", "recital", "choir", "gymnastics", "swim", "soccer", "rehearsal")


def _parse(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, AttributeError):
        raise InsightError(f"bad event time: {value!r}", 2)


def _days_of(ev: dict) -> list[date]:
    s, e = _parse(ev["start"]), _parse(ev["end"] if ev.get("end") else ev["start"])
    last = (e - timedelta(seconds=1)).date() if ev.get("all_day") and e > s else e.date()
    return [s.date() + timedelta(days=i) for i in range(max((last - s.date()).days, 0) + 1)]


def compute(events: list[dict], start: date, end: date) -> dict[str, dict]:
    """Metrics for every day in [start, end] from raw events."""
    out = {(start + timedelta(days=i)).isoformat(): {"events": 0, "spans": [], "caroline_work": 0,
                                                     "travel": 0, "kid_activity": 0}
           for i in range((end - start).days + 1)}
    for ev in events:
        cal, title = (ev.get("calendar") or "").lower(), (ev.get("title") or "").lower()
        if any(x in cal for x in EXCLUDED_CALENDARS):
            continue
        for d in _days_of(ev):
            row = out.get(d.isoformat())
            if row is None:
                continue
            row["events"] += 1
            if CAROLINE_WORK in title or CAROLINE_WORK in cal:
                row["caroline_work"] = 1
            if any(w in title for w in TRAVEL_WORDS):
                row["travel"] = 1
            if any(w in title for w in KID_WORDS):
                row["kid_activity"] = 1
            if not ev.get("all_day"):
                lo = max(_parse(ev["start"]), datetime.combine(d, datetime.min.time()))
                hi = min(_parse(ev["end"] or ev["start"]), datetime.combine(d + timedelta(days=1), datetime.min.time()))
                if hi > lo:
                    row["spans"].append((lo, hi))
    for row in out.values():
        row["hours_booked"] = round(_merged_hours(row.pop("spans")), 2)
    return out


def _merged_hours(spans: list[tuple[datetime, datetime]]) -> float:
    total, cur_lo, cur_hi = 0.0, None, None
    for lo, hi in sorted(spans):
        if cur_hi is None or lo > cur_hi:
            if cur_hi is not None:
                total += (cur_hi - cur_lo).total_seconds()
            cur_lo, cur_hi = lo, hi
        else:
            cur_hi = max(cur_hi, hi)
    if cur_hi is not None:
        total += (cur_hi - cur_lo).total_seconds()
    return total / 3600


def sync(events: list[dict], start: str, end: str, *, db_path=None) -> dict:
    """Replace the stored metrics for every day in [start, end] from the given events."""
    try:
        s, e = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        raise InsightError("start/end must be YYYY-MM-DD", 2)
    if e < s:
        raise InsightError("end is before start", 2)
    days = compute(events, s, e)
    now = datetime.now().isoformat(timespec="seconds")
    conn = _connect(db_path)
    try:
        for d, m in days.items():
            conn.execute(
                "INSERT INTO calendar_load (day, events, hours_booked, caroline_work, travel, kid_activity, synced_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET events=excluded.events, "
                "hours_booked=excluded.hours_booked, caroline_work=excluded.caroline_work, travel=excluded.travel, "
                "kid_activity=excluded.kid_activity, synced_at=excluded.synced_at",
                (d, m["events"], m["hours_booked"], m["caroline_work"], m["travel"], m["kid_activity"], now))
        conn.commit()
    finally:
        conn.close()
    busy = sum(1 for m in days.values() if m["events"])
    return {"days": len(days), "days_with_events": busy, "start": start, "end": end}


def status(*, db_path=None) -> dict:
    conn = _connect(db_path)
    try:
        r = conn.execute("SELECT COUNT(*) n, MIN(day) lo, MAX(day) hi FROM calendar_load").fetchone()
        return {"days": r["n"], "first_day": r["lo"], "last_day": r["hi"]}
    finally:
        conn.close()


def read_days(start: str, *, db_path=None) -> list[sqlite3.Row]:
    conn = _connect(db_path)
    try:
        return conn.execute("SELECT * FROM calendar_load WHERE day >= ? ORDER BY day", (start,)).fetchall()
    finally:
        conn.close()
