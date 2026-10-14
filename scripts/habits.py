"""Habit / streak tracker: the single shared service.

Zero-dependency (Python stdlib only), same SQLite file as state_store.py
(state/agent_results.db, schema in state/schema.sql).

Both front ends are thin wrappers over this module, so there is exactly one
implementation of the business rules (streaks, at-risk, logging, undo):
  - scripts/habits_store.py   the CLI the `habits` subagent runs
  - dashboard/routers/habits.py   the web UI and its /api/habits* twins

Every function takes an optional `db_path` (default: state/agent_results.db) and, where
dates matter, an optional `today` so tests can pin the clock. Failures raise HabitError
with a CLI-style code: 1 = not found, 2 = bad input.

Streak rules
  - Periods: daily = a calendar day; weekly = Monday through Sunday.
  - A period is "met" when its logged counts add up to at least target_count.
  - The current streak is the run of consecutive met periods ending now. The period still
    in progress counts if already met, but an unmet in-progress period does not break the
    streak yet; it only resets once the period ends unmet.
  - A habit with no target (target_count NULL) has no streak, just counts and frequency stats.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "state" / "agent_results.db"
SCHEMA_PATH = REPO_ROOT / "state" / "schema.sql"

FREQUENCIES = ("daily", "weekly")
CONFIG_DEFAULTS = {"nudge_enabled": "1"}


class HabitError(Exception):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.message = message
        self.code = code


# ---------------------------------------------------------------------------
# plumbing
# ---------------------------------------------------------------------------


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _today(today: date | None) -> date:
    return today or datetime.now().date()


def _parse_date(value: str | None, field: str = "date") -> date | None:
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise HabitError(f"invalid {field} {value!r}, expected YYYY-MM-DD") from None


def _clean_name(name: str | None) -> str:
    name = (name or "").strip()
    if not name:
        raise HabitError("name is required")
    return name


def _check_frequency(frequency: str) -> str:
    if frequency not in FREQUENCIES:
        raise HabitError(f"frequency must be one of {', '.join(FREQUENCIES)}")
    return frequency


def _check_target(target_count: int | None) -> int | None:
    if target_count is not None and target_count < 1:
        raise HabitError("target_count must be 1 or more (omit it for a no-goal habit)")
    return target_count


def _habit(conn: sqlite3.Connection, habit_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM habits WHERE id = ?", (habit_id,)).fetchone()
    if row is None:
        raise HabitError(f"no habit with id {habit_id}", 1)
    return row


# ---------------------------------------------------------------------------
# definitions
# ---------------------------------------------------------------------------


def create_habit(name: str, frequency: str, target_count: int | None = None, *, owner: str | None = None,
                 description: str | None = None, remind: bool = True, db_path=None) -> dict:
    name = _clean_name(name)
    _check_frequency(frequency)
    _check_target(target_count)
    now = _now_iso()
    new_id = str(uuid.uuid4())
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO habits (id, name, description, owner, frequency, target_count, remind, active, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (new_id, name, (description or None), (owner or "").strip() or None, frequency, target_count,
             1 if remind else 0, now, now),
        )
        conn.commit()
        return dict(_habit(conn, new_id))
    finally:
        conn.close()


_UNSET = object()


def update_habit(habit_id: str, *, name=_UNSET, description=_UNSET, owner=_UNSET, frequency=_UNSET,
                 target_count=_UNSET, remind=_UNSET, active=_UNSET, db_path=None) -> dict:
    """Update any subset of fields. Pass target_count=None to turn a goal into a no-goal habit
    and owner=None to make a habit shared."""
    fields: dict = {}
    if name is not _UNSET:
        fields["name"] = _clean_name(name)
    if description is not _UNSET:
        fields["description"] = description or None
    if owner is not _UNSET:
        fields["owner"] = (owner or "").strip() or None
    if frequency is not _UNSET:
        fields["frequency"] = _check_frequency(frequency)
    if target_count is not _UNSET:
        fields["target_count"] = _check_target(target_count)
    if remind is not _UNSET:
        fields["remind"] = 1 if remind else 0
    if active is not _UNSET:
        fields["active"] = 1 if active else 0
    if not fields:
        raise HabitError("no fields to update")
    fields["updated_at"] = _now_iso()
    conn = connect(db_path)
    try:
        _habit(conn, habit_id)
        conn.execute(f"UPDATE habits SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                     [*fields.values(), habit_id])
        conn.commit()
        return dict(_habit(conn, habit_id))
    finally:
        conn.close()


def resolve_habit(ref: str, db_path=None) -> dict:
    """Find a habit by id, exact name, or unique name substring (case-insensitive)."""
    ref = (ref or "").strip()
    if not ref:
        raise HabitError("habit is required")
    conn = connect(db_path)
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM habits ORDER BY name").fetchall()]
    finally:
        conn.close()
    low = ref.lower()
    for stage in (lambda h: h["id"] == ref, lambda h: h["name"].lower() == low,
                  lambda h: low in h["name"].lower()):
        hits = [h for h in rows if stage(h)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            names = ", ".join(f"{h['name']!r}" for h in hits)
            raise HabitError(f"{ref!r} matches more than one habit: {names}")
    raise HabitError(f"no habit matching {ref!r}", 1)


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------


def log_completion(habit_id: str, *, log_date: str | None = None, count: int = 1, note: str | None = None,
                   db_path=None, today: date | None = None) -> dict:
    """Record the habit as done. log_date defaults to today; backfill past dates is allowed,
    future dates are not."""
    if count < 1:
        raise HabitError("count must be 1 or more")
    today_d = _today(today)
    day = _parse_date(log_date, "log_date") or today_d
    if day > today_d:
        raise HabitError(f"log_date {day.isoformat()} is in the future")
    conn = connect(db_path)
    try:
        habit = _habit(conn, habit_id)
        new_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO habit_logs (id, habit_id, log_date, count, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (new_id, habit_id, day.isoformat(), count, note or None, _now_iso()),
        )
        conn.commit()
        logs = _logs(conn, habit_id)
        return {"log": dict(conn.execute("SELECT * FROM habit_logs WHERE id = ?", (new_id,)).fetchone()),
                "status": habit_status(dict(habit), logs, today_d)}
    finally:
        conn.close()


def undo_last(habit_id: str, *, db_path=None, today: date | None = None) -> dict:
    """Delete the most recently *entered* log (a mis-tap fix). Logs are otherwise append-only."""
    conn = connect(db_path)
    try:
        habit = _habit(conn, habit_id)
        row = conn.execute(
            "SELECT * FROM habit_logs WHERE habit_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (habit_id,)).fetchone()
        if row is None:
            raise HabitError(f"habit {habit['name']!r} has nothing logged to undo", 1)
        conn.execute("DELETE FROM habit_logs WHERE id = ?", (row["id"],))
        conn.commit()
        logs = _logs(conn, habit_id)
        return {"removed": dict(row), "status": habit_status(dict(habit), logs, _today(today))}
    finally:
        conn.close()


def list_logs(habit_id: str, *, limit: int | None = 20, db_path=None) -> list[dict]:
    conn = connect(db_path)
    try:
        _habit(conn, habit_id)
        sql = "SELECT * FROM habit_logs WHERE habit_id = ? ORDER BY log_date DESC, created_at DESC"
        params: list = [habit_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _logs(conn: sqlite3.Connection, habit_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT log_date, count FROM habit_logs WHERE habit_id = ? ORDER BY log_date", (habit_id,)).fetchall()]


# ---------------------------------------------------------------------------
# streak math (pure)
# ---------------------------------------------------------------------------


def period_start(frequency: str, day: date) -> date:
    return day if frequency == "daily" else day - timedelta(days=day.weekday())


def _step(frequency: str) -> timedelta:
    return timedelta(days=1 if frequency == "daily" else 7)


def period_end(frequency: str, start: date) -> date:
    return start + _step(frequency) - timedelta(days=1)


def habit_status(habit: dict, logs: list[dict], today: date) -> dict:
    """Compute streak/progress for one habit from its logs. Pure: no DB access."""
    freq = habit["frequency"]
    target = habit["target_count"]
    step = _step(freq)
    cur = period_start(freq, today)

    totals: dict[date, int] = {}
    days_done: set[date] = set()
    for lg in logs:
        d = datetime.strptime(lg["log_date"], "%Y-%m-%d").date()
        totals[period_start(freq, d)] = totals.get(period_start(freq, d), 0) + lg["count"]
        days_done.add(d)

    done_now = totals.get(cur, 0)
    out = {
        "id": habit["id"], "name": habit["name"], "description": habit.get("description"),
        "owner": habit.get("owner"), "frequency": freq, "target_count": target,
        "remind": bool(habit.get("remind", 1)), "active": bool(habit.get("active", 1)),
        "has_goal": target is not None,
        "done_this_period": done_now,
        "period_start": cur.isoformat(), "period_end": period_end(freq, cur).isoformat(),
        "last_done": max(days_done).isoformat() if days_done else None,
        "total_count": sum(totals.values()),
    }

    if target is None:
        week = period_start("weekly", today)
        gaps = [(b - a).days for a, b in zip(sorted(days_done), sorted(days_done)[1:])]
        out.update({
            "current_streak": None, "longest_streak": None, "period_status": "tracking", "at_risk": False,
            "count_this_week": sum(lg["count"] for lg in logs
                                   if datetime.strptime(lg["log_date"], "%Y-%m-%d").date() >= week),
            "count_last_30_days": sum(lg["count"] for lg in logs
                                      if datetime.strptime(lg["log_date"], "%Y-%m-%d").date()
                                      > today - timedelta(days=30)),
            "avg_gap_days": round(sum(gaps) / len(gaps), 1) if gaps else None,
        })
        return out

    def met(p: date) -> bool:
        return totals.get(p, 0) >= target

    # current streak: walk back from the current period (or the one before it if still unmet)
    p = cur if met(cur) else cur - step
    current = 0
    while met(p):
        current += 1
        p -= step
    streak_before_now = current - (1 if met(cur) else 0)

    # longest streak over the whole history
    longest = run = 0
    if totals:
        p = min(totals)
        while p <= cur:
            run = run + 1 if met(p) else 0
            longest = max(longest, run)
            p += step

    remaining = max(0, target - done_now)
    days_left = (period_end(freq, cur) - today).days + 1
    if remaining == 0:
        status = "met"
    elif days_left <= remaining + 1 and streak_before_now > 0:
        status = "at_risk"
    else:
        status = "open"
    out.update({
        "current_streak": current, "longest_streak": max(longest, current), "period_status": status,
        "at_risk": status == "at_risk", "remaining": remaining, "days_left_in_period": days_left,
    })
    return out


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def get_status(habit_id: str, *, db_path=None, today: date | None = None) -> dict:
    conn = connect(db_path)
    try:
        habit = dict(_habit(conn, habit_id))
        return habit_status(habit, _logs(conn, habit_id), _today(today))
    finally:
        conn.close()


def list_status(*, owner: str | None = None, include_inactive: bool = False, db_path=None,
                today: date | None = None) -> list[dict]:
    """All habits with computed streak/progress. owner filters to that owner plus shared (no-owner)
    habits."""
    today_d = _today(today)
    conn = connect(db_path)
    try:
        sql, params = "SELECT * FROM habits WHERE 1=1", []
        if not include_inactive:
            sql += " AND active = 1"
        if owner:
            sql += " AND (owner = ? COLLATE NOCASE OR owner IS NULL)"
            params.append(owner)
        sql += " ORDER BY name COLLATE NOCASE"
        habits = [dict(r) for r in conn.execute(sql, params).fetchall()]
        return [habit_status(h, _logs(conn, h["id"]), today_d) for h in habits]
    finally:
        conn.close()


def get_at_risk(*, for_nudge: bool = False, db_path=None, today: date | None = None) -> list[dict]:
    """Habits whose live streak breaks if not done soon. for_nudge=True applies the nudge
    settings (global nudge_enabled switch plus each habit's remind flag)."""
    if for_nudge and get_config(db_path=db_path)["nudge_enabled"] != "1":
        return []
    items = [s for s in list_status(db_path=db_path, today=today) if s["at_risk"]]
    if for_nudge:
        items = [s for s in items if s["remind"]]
    return items


# ---------------------------------------------------------------------------
# calendars and analysis (read-only views over habit_logs)
# ---------------------------------------------------------------------------


def month_grid(counts: dict[str, int], year: int, month: int, today: date) -> dict:
    """One Monday-first month as weeks of 7 cells (None = padding). counts maps ISO date -> count."""
    first = date(year, month, 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    cells: list = [None] * first.weekday()
    for n in range((nxt - first).days):
        d = first + timedelta(days=n)
        iso = d.isoformat()
        cells.append({"date": iso, "day": d.day, "count": counts.get(iso, 0),
                      "today": d == today, "future": d > today})
    cells += [None] * (-len(cells) % 7)
    return {"year": year, "month": month, "label": first.strftime("%B %Y"),
            "weeks": [cells[i:i + 7] for i in range(0, len(cells), 7)]}


def _months_back(today: date, months: int) -> list[tuple[int, int]]:
    """(year, month) pairs, oldest first, ending with today's month."""
    out = []
    y, m = today.year, today.month
    for _ in range(max(months, 1)):
        out.append((y, m))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out[::-1]


def _counts_by_day(conn: sqlite3.Connection, habit_id: str | None, since: date) -> dict[str, int]:
    """ISO date -> logged count (one habit) or number of distinct active habits done (habit_id None)."""
    if habit_id:
        rows = conn.execute("SELECT log_date, SUM(count) AS n FROM habit_logs WHERE habit_id = ? AND log_date >= ? "
                            "GROUP BY log_date", (habit_id, since.isoformat())).fetchall()
    else:
        rows = conn.execute("SELECT l.log_date, COUNT(DISTINCT l.habit_id) AS n FROM habit_logs l "
                            "JOIN habits h ON h.id = l.habit_id WHERE h.active = 1 AND l.log_date >= ? "
                            "GROUP BY l.log_date", (since.isoformat(),)).fetchall()
    return {r[0]: r[1] for r in rows}


def calendar(habit_id: str | None = None, *, months: int = 2, db_path=None, today: date | None = None) -> dict:
    """Month grids showing which days a habit was done (habit_id None = habits done per day across all
    active habits). months counts back from the current month."""
    today_d = _today(today)
    span = _months_back(today_d, months)
    conn = connect(db_path)
    try:
        name = _habit(conn, habit_id)["name"] if habit_id else "All habits"
        counts = _counts_by_day(conn, habit_id, date(span[0][0], span[0][1], 1))
    finally:
        conn.close()
    grids = [month_grid(counts, y, m, today_d) for y, m in span]
    return {"habit_id": habit_id, "name": name, "months": grids,
            "days_done": sum(1 for g in grids for w in g["weeks"] for c in w if c and c["count"]),
            "max_count": max(counts.values(), default=0)}


def analysis(*, weeks: int = 12, db_path=None, today: date | None = None) -> dict:
    """Cross-habit rollup for the analysis page: per-habit totals, weekday pattern and a weekly strip,
    plus the combined per-day calendar."""
    today_d = _today(today)
    start = period_start("weekly", today_d) - timedelta(weeks=max(weeks, 1) - 1)
    conn = connect(db_path)
    try:
        habits_ = [dict(r) for r in conn.execute(
            "SELECT * FROM habits WHERE active = 1 ORDER BY name COLLATE NOCASE").fetchall()]
        combined = _counts_by_day(conn, None, start)
        rows = []
        for h in habits_:
            counts = _counts_by_day(conn, h["id"], start)
            days = [date.fromisoformat(k) for k, v in counts.items() if v]
            weekday = [0] * 7
            for d in days:
                weekday[d.weekday()] += 1
            strip = []
            for i in range(weeks):
                ws = start + timedelta(weeks=i)
                strip.append(sum(1 for d in days if ws <= d < ws + timedelta(days=7)))
            last30 = sum(1 for d in days if d > today_d - timedelta(days=30))
            rows.append({"id": h["id"], "name": h["name"], "owner": h["owner"], "target_count": h["target_count"],
                         "frequency": h["frequency"], "days_done": len(days), "last_30_days": last30,
                         "weekday_counts": weekday, "weekly_days": strip,
                         "best_weekday": ("Mon Tue Wed Thu Fri Sat Sun".split()[weekday.index(max(weekday))]
                                          if days else None)})
    finally:
        conn.close()
    week_totals = [sum(v for k, v in combined.items()
                       if start + timedelta(weeks=i) <= date.fromisoformat(k) < start + timedelta(weeks=i + 1))
                   for i in range(weeks)]
    grids = [month_grid(combined, y, m, today_d) for y, m in _months_back(today_d, 3)]
    return {"weeks": weeks, "since": start.isoformat(), "habits": rows, "months": grids,
            "week_totals": week_totals, "max_count": max(combined.values(), default=0),
            "active_days": sum(1 for v in combined.values() if v)}


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def get_config(*, db_path=None) -> dict:
    conn = connect(db_path)
    try:
        stored = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM habit_config")}
    finally:
        conn.close()
    return {**CONFIG_DEFAULTS, **stored}


def set_config(key: str, value: str, *, db_path=None) -> dict:
    if key not in CONFIG_DEFAULTS:
        raise HabitError(f"unknown config key {key!r}; known: {', '.join(CONFIG_DEFAULTS)}")
    if key == "nudge_enabled" and str(value) not in ("0", "1"):
        raise HabitError("nudge_enabled must be 0 or 1")
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO habit_config (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, str(value), _now_iso()))
        conn.commit()
    finally:
        conn.close()
    return get_config(db_path=db_path)
