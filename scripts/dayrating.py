"""Day rating: the single shared service for the 1-10 "how was your day" score plus a short note.

Zero-dependency (Python stdlib only), same SQLite file as state_store.py (state/agent_results.db,
schema in state/schema.sql). Both front ends are thin wrappers over this module:
  - scripts/dayrating_store.py   the CLI the `dayrating` subagent runs
  - dashboard/routers/dayrating.py   the web UI and its /api/rating* twins

One rating per day; rating a day again edits it. Past days may be backfilled, future days may not.
Every function takes an optional `db_path` and, where dates matter, an optional `today`.
Failures raise RatingError with a CLI-style code: 1 = not found, 2 = bad input.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "state" / "agent_results.db"
SCHEMA_PATH = REPO_ROOT / "state" / "schema.sql"

MIN_SCORE, MAX_SCORE = 1, 10
NOTE_MAX = 500


class RatingError(Exception):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.message = message
        self.code = code


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


def _parse_date(value: str | None, today: date) -> date:
    if not value:
        return today
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise RatingError(f"invalid date {value!r}, expected YYYY-MM-DD") from None


def _row(r: sqlite3.Row) -> dict:
    return {"date": r["rating_date"], "score": r["score"], "note": r["note"],
            "created_at": r["created_at"], "updated_at": r["updated_at"]}


def _check_score(score) -> int:
    try:
        value = int(score)
    except (TypeError, ValueError):
        raise RatingError(f"score must be a whole number {MIN_SCORE}-{MAX_SCORE}") from None
    if not MIN_SCORE <= value <= MAX_SCORE:
        raise RatingError(f"score must be between {MIN_SCORE} and {MAX_SCORE}")
    return value


def set_rating(score, note: str | None = None, *, rating_date: str | None = None, db_path=None,
               today: date | None = None) -> dict:
    """Create or replace the rating for a day (default today). An omitted note keeps an existing note."""
    today = _today(today)
    day = _parse_date(rating_date, today)
    if day > today:
        raise RatingError("can't rate a future day")
    value = _check_score(score)
    note = (note or "").strip()[:NOTE_MAX] or None
    conn = connect(db_path)
    try:
        existing = conn.execute("SELECT * FROM day_ratings WHERE rating_date = ?", (day.isoformat(),)).fetchone()
        now = _now_iso()
        if existing:
            conn.execute("UPDATE day_ratings SET score = ?, note = ?, updated_at = ? WHERE id = ?",
                         (value, note if note is not None else existing["note"], now, existing["id"]))
        else:
            conn.execute("INSERT INTO day_ratings (id, rating_date, score, note, created_at, updated_at) "
                         "VALUES (?,?,?,?,?,?)", (str(uuid.uuid4()), day.isoformat(), value, note, now, now))
        conn.commit()
        return _row(conn.execute("SELECT * FROM day_ratings WHERE rating_date = ?", (day.isoformat(),)).fetchone())
    finally:
        conn.close()


def get_rating(rating_date: str | None = None, *, db_path=None, today: date | None = None) -> dict | None:
    day = _parse_date(rating_date, _today(today))
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT * FROM day_ratings WHERE rating_date = ?", (day.isoformat(),)).fetchone()
        return _row(r) if r else None
    finally:
        conn.close()


def delete_rating(rating_date: str, *, db_path=None, today: date | None = None) -> dict:
    day = _parse_date(rating_date, _today(today))
    conn = connect(db_path)
    try:
        cur = conn.execute("DELETE FROM day_ratings WHERE rating_date = ?", (day.isoformat(),))
        conn.commit()
        if not cur.rowcount:
            raise RatingError(f"no rating for {day.isoformat()}", 1)
        return {"deleted": day.isoformat()}
    finally:
        conn.close()


def list_ratings(*, days: int | None = 60, db_path=None, today: date | None = None) -> list[dict]:
    """Ratings newest first; `days` limits to the last N days (today included), None = all."""
    conn = connect(db_path)
    try:
        if days is None:
            rows = conn.execute("SELECT * FROM day_ratings ORDER BY rating_date DESC").fetchall()
        else:
            since = (_today(today) - timedelta(days=days - 1)).isoformat()
            rows = conn.execute("SELECT * FROM day_ratings WHERE rating_date >= ? ORDER BY rating_date DESC",
                                (since,)).fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()


def status(*, db_path=None, today: date | None = None) -> dict:
    """Today's rating (or None), whether yesterday is unrated, averages and the logging streak."""
    today = _today(today)
    ratings = {r["date"]: r for r in list_ratings(days=None, db_path=db_path, today=today)}

    def avg(n: int):
        vals = [r["score"] for d, r in ratings.items()
                if today - timedelta(days=n - 1) <= date.fromisoformat(d) <= today]
        return round(sum(vals) / len(vals), 1) if vals else None

    # Streak of consecutive rated days; an unrated today doesn't break it yet.
    streak, day = 0, today if today.isoformat() in ratings else today - timedelta(days=1)
    while day.isoformat() in ratings:
        streak += 1
        day -= timedelta(days=1)
    yesterday = (today - timedelta(days=1)).isoformat()
    return {"today": ratings.get(today.isoformat()), "rated_today": today.isoformat() in ratings,
            "yesterday_unrated": yesterday not in ratings, "avg_7": avg(7), "avg_30": avg(30),
            "total_ratings": len(ratings), "logging_streak": streak}


def needs_nudge(*, db_path=None, today: date | None = None) -> dict:
    """What the evening nudge should say: nothing (rated today) or ask for today's rating."""
    s = status(db_path=db_path, today=today)
    return {"nudge": not s["rated_today"], "logging_streak": s["logging_streak"],
            "yesterday_unrated": s["yesterday_unrated"]}
