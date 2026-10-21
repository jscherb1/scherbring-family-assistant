"""Journal + nightly check-in: the single shared service for Whoop-style daily yes/no questions and the one
evening message that gathers everything the assistant needs from the user that day.

Zero-dependency (Python stdlib only), same SQLite file as the rest of the assistant (tables journal_questions,
journal_answers in state/schema.sql). Both front ends are thin wrappers over this module:
  - scripts/journal_store.py        the CLI the `journal` subagent runs
  - dashboard/routers/journal.py    the check-in card and Journal page in the Wellness section, and /api/journal*

Answers are explicit: yes (1), no (0) or unanswered (no row). Unanswered days stay out of the analysis, which is
why this replaces count-only habits for behaviours: a habit can't tell "didn't do it" from "didn't log it".

The nightly check-in (`pending`, `checkin_message`, `record`) gathers, for one day: the day rating
(dayrating.py), every active journal question, and daily habits not yet logged (habits.py). Adding another
nightly input means adding it to `pending`, `checkin_message` and `record` here; both front ends follow.

Every function takes an optional `db_path` and, where dates matter, `today` / `now`.
Failures raise JournalError with a CLI-style code: 1 = not found, 2 = bad input.
"""

from __future__ import annotations

import re
import sqlite3
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import dayrating  # noqa: E402
import habits  # noqa: E402
import insights  # noqa: E402

# (slug, question, category). Seeded the first time the journal is used.
DEFAULT_QUESTIONS = [
    ("alcohol", "Alcohol?", "substances"),
    ("caffeine_late", "Caffeine after 2 PM?", "substances"),
    ("thc", "THC?", "substances"),
    ("screens_bed", "Screens in bed?", "sleep"),
    ("late_meal", "Late meal?", "food"),
    ("sick", "Sick or sore?", "health"),
    ("stressful", "Stressful day?", "health"),
    ("sex", "Sex?", "social"),
    ("date_night", "Date night?", "social"),
]
CHECKIN_FROM_HOUR = 17      # from 5 PM the dashboard check-in card opens expanded
MIN_EACH = 5                # yes days AND no days needed before a question's impact is shown (Whoop uses 5)
# Impact targets: (key, insight metric or None for the day rating, label, unit, lag days, higher is better).
# Overnight metrics are filed on the wake-up date, so a behaviour on day D shows in the night ending D+1.
IMPACT_TARGETS = [
    ("rating", None, "Day rating", "pts", 0, True),
    ("hrv", "coros:hrv_ms", "Next-night HRV", "ms", 1, True),
    ("sleep_score", "coros:sleep_score", "Next-night sleep score", "", 1, True),
    ("resting_hr", "coros:resting_hr", "Next-day resting HR", "bpm", 1, False),
]


class JournalError(Exception):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.message = message
        self.code = code


def connect(db_path=None) -> sqlite3.Connection:
    conn = dayrating.connect(db_path)
    if conn.execute("SELECT COUNT(*) FROM journal_questions").fetchone()[0] == 0:
        now = _now_iso()
        for i, (slug, text, cat) in enumerate(DEFAULT_QUESTIONS):
            conn.execute("INSERT INTO journal_questions (id, slug, text, category, active, sort, created_at, updated_at) "
                         "VALUES (?,?,?,?,1,?,?,?)", (str(uuid.uuid4()), slug, text, cat, i, now, now))
        conn.commit()
    return conn


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _today(today: date | None) -> date:
    return today or datetime.now().date()


def _day(value, today: date) -> date:
    if not value:
        return today
    if isinstance(value, date):
        d = value
    else:
        try:
            d = datetime.strptime(str(value), "%Y-%m-%d").date()
        except ValueError:
            raise JournalError(f"invalid date {value!r}, expected YYYY-MM-DD") from None
    if d > today:
        raise JournalError(f"{d.isoformat()} is in the future")
    return d


def _yes_no(value) -> int:
    v = str(value).strip().lower() if value is not None else ""
    if v in ("1", "yes", "y", "true", "on"):
        return 1
    if v in ("0", "no", "n", "false", "off"):
        return 0
    raise JournalError(f"answer must be yes or no, not {value!r}")


def label(q: dict) -> str:
    """'Alcohol?' -> 'Alcohol' for lists."""
    return q["text"].rstrip("?").strip()


# ---------------------------------------------------------------------------
# questions
# ---------------------------------------------------------------------------


def list_questions(*, include_inactive: bool = False, db_path=None) -> list[dict]:
    conn = connect(db_path)
    try:
        sql = "SELECT * FROM journal_questions" + ("" if include_inactive else " WHERE active = 1")
        return [dict(r) for r in conn.execute(sql + " ORDER BY sort, created_at")]
    finally:
        conn.close()


def resolve_question(ref: str, *, db_path=None) -> dict:
    """Find a question by id, slug, or a unique fragment of its slug or text (case-insensitive)."""
    ref = (ref or "").strip()
    if not ref:
        raise JournalError("question is required")
    rows = list_questions(include_inactive=True, db_path=db_path)
    low = ref.lower().replace(" ", "_")
    for stage in (lambda q: q["id"] == ref or q["slug"] == low,
                  lambda q: label(q).lower() == ref.lower(),
                  lambda q: low in q["slug"] or ref.lower() in q["text"].lower()):
        hits = [q for q in rows if stage(q)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise JournalError(f"{ref!r} matches more than one question: {', '.join(q['slug'] for q in hits)}")
    raise JournalError(f"no question matching {ref!r}", 1)


def add_question(text: str, *, slug: str | None = None, category: str | None = None, db_path=None) -> dict:
    text = (text or "").strip()
    if not text:
        raise JournalError("question text is required")
    if not text.endswith("?"):
        text += "?"
    slug = re.sub(r"[^a-z0-9]+", "_", (slug or text).lower()).strip("_")[:30]
    if not slug:
        raise JournalError("question needs a slug of letters or numbers")
    conn = connect(db_path)
    try:
        if conn.execute("SELECT 1 FROM journal_questions WHERE slug = ?", (slug,)).fetchone():
            raise JournalError(f"a question with slug {slug!r} already exists")
        sort = conn.execute("SELECT COALESCE(MAX(sort), -1) + 1 FROM journal_questions").fetchone()[0]
        qid, now = str(uuid.uuid4()), _now_iso()
        conn.execute("INSERT INTO journal_questions (id, slug, text, category, active, sort, created_at, updated_at) "
                     "VALUES (?,?,?,?,1,?,?,?)", (qid, slug, text, category or None, sort, now, now))
        conn.commit()
        return dict(conn.execute("SELECT * FROM journal_questions WHERE id = ?", (qid,)).fetchone())
    finally:
        conn.close()


def update_question(ref: str, *, text: str | None = None, category: str | None = None, active: bool | None = None,
                    db_path=None) -> dict:
    q = resolve_question(ref, db_path=db_path)
    changes = {}
    if text is not None:
        if not text.strip():
            raise JournalError("question text can't be empty")
        changes["text"] = text.strip() if text.strip().endswith("?") else text.strip() + "?"
    if category is not None:
        changes["category"] = category.strip() or None
    if active is not None:
        changes["active"] = 1 if active else 0
    if not changes:
        raise JournalError("nothing to change")
    conn = connect(db_path)
    try:
        sets = ", ".join(f"{k} = ?" for k in changes)
        conn.execute(f"UPDATE journal_questions SET {sets}, updated_at = ? WHERE id = ?",
                     (*changes.values(), _now_iso(), q["id"]))
        conn.commit()
        return dict(conn.execute("SELECT * FROM journal_questions WHERE id = ?", (q["id"],)).fetchone())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# answers
# ---------------------------------------------------------------------------


def set_answer(ref: str, value, *, answer_date=None, source: str = "chat", db_path=None,
               today: date | None = None) -> dict:
    """Record yes/no for one question on one day (default today). Answering again changes it."""
    day = _day(answer_date, _today(today)).isoformat()
    v = _yes_no(value)
    q = resolve_question(ref, db_path=db_path)
    conn = connect(db_path)
    try:
        now = _now_iso()
        conn.execute("INSERT INTO journal_answers (id, question_id, answer_date, value, source, created_at, updated_at) "
                     "VALUES (?,?,?,?,?,?,?) ON CONFLICT(question_id, answer_date) DO UPDATE SET "
                     "value = excluded.value, source = excluded.source, updated_at = excluded.updated_at",
                     (str(uuid.uuid4()), q["id"], day, v, source, now, now))
        conn.commit()
        return {"question": q["slug"], "date": day, "answer": "yes" if v else "no"}
    finally:
        conn.close()


def clear_answer(ref: str, *, answer_date=None, db_path=None, today: date | None = None) -> dict:
    day = _day(answer_date, _today(today)).isoformat()
    q = resolve_question(ref, db_path=db_path)
    conn = connect(db_path)
    try:
        n = conn.execute("DELETE FROM journal_answers WHERE question_id = ? AND answer_date = ?", (q["id"], day)).rowcount
        conn.commit()
        if not n:
            raise JournalError(f"{q['slug']} has no answer for {day}", 1)
        return {"question": q["slug"], "date": day, "cleared": True}
    finally:
        conn.close()


def answers_for(answer_date=None, *, db_path=None, today: date | None = None) -> dict[str, int]:
    """{slug: 1|0} for one day; unanswered questions are absent."""
    day = _day(answer_date, _today(today)).isoformat()
    conn = connect(db_path)
    try:
        return {r["slug"]: r["value"] for r in conn.execute(
            "SELECT q.slug, a.value FROM journal_answers a JOIN journal_questions q ON q.id = a.question_id "
            "WHERE a.answer_date = ?", (day,))}
    finally:
        conn.close()


def series(*, db_path=None) -> dict[str, dict[str, float]]:
    """Every question's answered days as {slug: {day: 1.0|0.0}} (unanswered days absent)."""
    conn = connect(db_path)
    try:
        out: dict[str, dict[str, float]] = {}
        for r in conn.execute("SELECT q.slug, a.answer_date d, a.value v FROM journal_answers a "
                              "JOIN journal_questions q ON q.id = a.question_id"):
            out.setdefault(r["slug"], {})[r["d"]] = float(r["v"])
        return out
    finally:
        conn.close()


def history(*, days: int = 28, db_path=None, today: date | None = None) -> dict:
    """A questions x days grid of yes / no / unanswered (oldest day first)."""
    if not 1 <= int(days) <= 366:
        raise JournalError("days must be 1-366")
    today = _today(today)
    dates = [(today - timedelta(days=i)).isoformat() for i in range(int(days) - 1, -1, -1)]
    s = series(db_path=db_path)
    rows = []
    for q in list_questions(include_inactive=True, db_path=db_path):
        vals = s.get(q["slug"], {})
        if not q["active"] and not any(d in vals for d in dates):
            continue
        cells = [{"date": d, "answer": None if d not in vals else ("yes" if vals[d] else "no")} for d in dates]
        rows.append({"slug": q["slug"], "text": q["text"], "label": label(q), "active": bool(q["active"]),
                     "category": q["category"], "cells": cells,
                     "yes": sum(1 for c in cells if c["answer"] == "yes"),
                     "no": sum(1 for c in cells if c["answer"] == "no")})
    answered = {d for r in rows for c in r["cells"] if c["answer"] for d in [c["date"]]}
    return {"dates": dates, "questions": rows, "answered_days": len(answered), "days": len(dates)}


def import_from_habit(habit_ref: str, question_ref: str, *, pause_habit: bool = True, db_path=None) -> dict:
    """Move a count-only habit into the journal: every day it was logged becomes a 'yes'. Earlier unlogged
    days stay unanswered (we can't know they were 'no'). Existing answers are kept. Pauses the habit."""
    h = habits.resolve_habit(habit_ref, db_path=db_path)
    q = resolve_question(question_ref, db_path=db_path)
    logged = sorted({lg["log_date"] for lg in habits.list_logs(h["id"], limit=None, db_path=db_path)})
    conn = connect(db_path)
    added = 0
    try:
        now = _now_iso()
        for d in logged:
            added += conn.execute(
                "INSERT OR IGNORE INTO journal_answers (id, question_id, answer_date, value, source, created_at, "
                "updated_at) VALUES (?,?,?,1,?,?,?)", (str(uuid.uuid4()), q["id"], d, f"import:{h['name']}", now, now)
            ).rowcount
        conn.commit()
    finally:
        conn.close()
    if pause_habit and h["active"]:
        habits.update_habit(h["id"], active=False, db_path=db_path)
    return {"habit": h["name"], "question": q["slug"], "days_logged": len(logged), "answers_added": added,
            "habit_paused": bool(pause_habit)}


# ---------------------------------------------------------------------------
# the nightly check-in
# ---------------------------------------------------------------------------


def checkin_date(now: datetime | None = None, *, db_path=None) -> date:
    """Which day the check-in is about: always today. An unfinished yesterday is surfaced separately
    (pending()["previous_day_unrated"]) rather than taking over the default."""
    return (now or datetime.now()).date()


def _daily_habits(day: date, today: date, db_path) -> list[dict]:
    out = []
    for h in habits.list_status(db_path=db_path, today=today):
        if h["has_goal"] and h["frequency"] == "daily":
            logs = habits.list_logs(h["id"], limit=None, db_path=db_path)
            out.append({"id": h["id"], "name": h["name"], "done": any(lg["log_date"] == day.isoformat() for lg in logs)})
    return out


def pending(day=None, *, today: date | None = None, db_path=None) -> dict:
    """Everything the check-in gathers for one day and what's still missing."""
    today = _today(today)
    d = _day(day, today)
    rating = dayrating.get_rating(d.isoformat(), db_path=db_path, today=today)
    answers = answers_for(d, db_path=db_path, today=today)
    qs = [{"slug": q["slug"], "text": q["text"], "label": label(q), "category": q["category"],
           "answer": None if q["slug"] not in answers else ("yes" if answers[q["slug"]] else "no")}
          for q in list_questions(db_path=db_path)]
    hs = _daily_habits(d, today, db_path)
    prev = d - timedelta(days=1)
    unanswered = [q["slug"] for q in qs if q["answer"] is None]
    return {"date": d.isoformat(), "is_today": d == today, "rating": rating, "rating_needed": rating is None,
            "previous_day_unrated": dayrating.get_rating(prev.isoformat(), db_path=db_path, today=today) is None,
            "questions": qs, "unanswered": unanswered, "habits": hs, "habits_open": [h["name"] for h in hs if not h["done"]],
            "complete": rating is not None and not unanswered and all(h["done"] for h in hs)}


def checkin_message(*, now: datetime | None = None, db_path=None) -> dict:
    """The nightly message, or {"message": None} when everything for today is already in. The scheduled task
    sends this text exactly as given."""
    now = now or datetime.now()
    p = pending(now.date(), today=now.date(), db_path=db_path)
    if p["complete"]:
        return {"message": None, "pending": p}
    when = datetime.strptime(p["date"], "%Y-%m-%d").strftime("%A, %b %-d")
    lines, n = [f"🌙 Check-in for {when}"], 0
    if p["rating_needed"]:
        n += 1
        lines.append(f"{n}) Rate today 1-10, with a few words on why")
    if p["unanswered"]:
        n += 1
        names = [q["label"] for q in p["questions"] if q["answer"] is None]
        lines.append(f"{n}) Yes or no today: " + " · ".join(names))
    if p["habits_open"]:
        n += 1
        lines.append(f"{n}) Not logged yet: {', '.join(p['habits_open'])}. Done any?")
    if p["previous_day_unrated"]:
        lines.append("Yesterday isn't rated yet either; add it if you like (\"yesterday 6\").")
    lines.append('Reply in one line, e.g. "7, busy but good. alcohol and late meal yes, rest no, did stretching". '
                 "Anything you skip stays blank.")
    return {"message": "\n".join(lines), "pending": p}


def record(*, day=None, score=None, note: str | None = None, yes=(), no=(), rest: str | None = None,
           habits_done=(), source: str = "chat", db_path=None, today: date | None = None) -> dict:
    """Apply a check-in reply in one go: rating (+ note), yes/no answers, `rest` ('yes'/'no') for every question
    still unanswered after that, and daily habits done (logged only if not already logged that day).
    Validates everything before writing anything."""
    today = _today(today)
    d = _day(day, today)
    yes_q = [resolve_question(r, db_path=db_path) for r in yes if r and r.strip()]
    no_q = [resolve_question(r, db_path=db_path) for r in no if r and r.strip()]
    both = {q["slug"] for q in yes_q} & {q["slug"] for q in no_q}
    if both:
        raise JournalError(f"answered both yes and no: {', '.join(sorted(both))}")
    rest_v = _yes_no(rest) if rest not in (None, "") else None
    try:
        hs = [habits.resolve_habit(r, db_path=db_path) for r in habits_done if r and r.strip()]
    except habits.HabitError as exc:
        raise JournalError(exc.message, exc.code) from None
    if score in (None, "") and note:  # a note on its own updates the existing rating's note
        existing = dayrating.get_rating(d.isoformat(), db_path=db_path, today=today)
        score = existing["score"] if existing else None
        if score is None:
            raise JournalError("a note needs a rating 1-10 to go with it")
    if score not in (None, ""):
        try:
            dayrating._check_score(score)
        except dayrating.RatingError as exc:
            raise JournalError(exc.message) from None
    written = {"rating": None, "answers": {}, "habits_logged": []}
    try:
        if score not in (None, ""):
            written["rating"] = dayrating.set_rating(score, note or None, rating_date=d.isoformat(), db_path=db_path,
                                                     today=today)["score"]
        for q, v in [(q, 1) for q in yes_q] + [(q, 0) for q in no_q]:
            set_answer(q["id"], v, answer_date=d, source=source, db_path=db_path, today=today)
            written["answers"][q["slug"]] = "yes" if v else "no"
        if rest_v is not None:
            for slug in pending(d, today=today, db_path=db_path)["unanswered"]:
                set_answer(slug, rest_v, answer_date=d, source=source, db_path=db_path, today=today)
                written["answers"][slug] = "yes" if rest_v else "no"
        open_ids = {h["id"] for h in _daily_habits(d, today, db_path) if not h["done"]}
        for h in hs:
            if h["id"] in open_ids:
                habits.log_completion(h["id"], log_date=d.isoformat(), note=f"check-in ({source})", db_path=db_path,
                                      today=today)
                written["habits_logged"].append(h["name"])
    except (dayrating.RatingError, habits.HabitError) as exc:
        raise JournalError(exc.message, exc.code) from None
    return {"recorded": written, "pending": pending(d, today=today, db_path=db_path)}


# ---------------------------------------------------------------------------
# impact (Whoop's "impact on recovery")
# ---------------------------------------------------------------------------


def _targets(db_path, start: str, end: str) -> dict[str, dict[str, float]]:
    conn = connect(db_path)
    try:
        out = {"rating": {r["rating_date"]: float(r["score"]) for r in conn.execute(
            "SELECT rating_date, score FROM day_ratings WHERE rating_date BETWEEN ? AND ?", (start, end))}}
        for key, metric, *_ in IMPACT_TARGETS:
            if metric:
                out[key] = {r["day"]: r["value"] for r in conn.execute(
                    "SELECT day, value FROM insight_daily WHERE metric = ? AND day BETWEEN ? AND ? AND value != 0",
                    (metric, start, end))}
        return out
    finally:
        conn.close()


def impact(*, days: int = 90, db_path=None, today: date | None = None) -> dict:
    """For each question: the average of each target on 'yes' days vs 'no' days (overnight targets one day
    later), shown once there are MIN_EACH of both. Permutation p-value and confidence come from insights."""
    if not 14 <= int(days) <= 366:
        raise JournalError("days must be 14-366")
    today = _today(today)
    start = (today - timedelta(days=int(days) - 1)).isoformat()
    tg = _targets(db_path, start, (today + timedelta(days=1)).isoformat())
    s = series(db_path=db_path)
    out = []
    for q in list_questions(db_path=db_path):
        ans = {d: v for d, v in s.get(q["slug"], {}).items() if start <= d <= today.isoformat()}
        row = {"slug": q["slug"], "label": label(q), "yes_days": sum(1 for v in ans.values() if v),
               "no_days": sum(1 for v in ans.values() if not v), "targets": []}
        for key, _, tlabel, unit, lag, up in IMPACT_TARGETS:
            pairs = [(v, tg[key][(date.fromisoformat(d) + timedelta(days=lag)).isoformat()]) for d, v in ans.items()
                     if (date.fromisoformat(d) + timedelta(days=lag)).isoformat() in tg[key]]
            with_ = [y for x, y in pairs if x]
            without = [y for x, y in pairs if not x]
            t = {"target": key, "label": tlabel, "unit": unit, "lag_days": lag, "yes_n": len(with_),
                 "no_n": len(without), "enough": len(with_) >= MIN_EACH and len(without) >= MIN_EACH}
            if t["enough"]:
                aw, ao = sum(with_) / len(with_), sum(without) / len(without)
                stat = insights.analyze_pair([x for x, _ in pairs], [y for _, y in pairs], min_days=2 * MIN_EACH)
                t.update(avg_with=round(aw, 1), avg_without=round(ao, 1), effect=round(aw - ao, 1),
                         pct=round((aw - ao) / ao * 100) if ao else None,
                         p_value=stat["p_value"] if stat else None,
                         confidence=stat["confidence"] if stat else "none",
                         verdict="good" if (aw > ao) == up and aw != ao else "bad" if aw != ao else "neutral")
            row["targets"].append(t)
        out.append(row)
    return {"days": int(days), "min_each": MIN_EACH, "questions": out,
            "targets": [{"key": k, "label": lab, "unit": u, "lag_days": lag} for k, _, lab, u, lag, _ in IMPACT_TARGETS]}
