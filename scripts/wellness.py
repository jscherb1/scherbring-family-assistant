"""Wellness: the single shared service behind the Wellness dashboard and the `wellness` subagent.

Zero-dependency (Python stdlib only), same SQLite file as the rest of the assistant. It writes nothing:
it reads the daily metrics the insights sources already collect (insight_daily: COROS sleep, HRV, resting
heart rate, load, steps, stress), the day ratings and the habits, and turns them into a readiness view of
today, trend series with personal baselines, and "what moves X" findings. Both front ends are thin
wrappers over this module:
  - scripts/wellness_store.py      the CLI the `wellness` subagent runs
  - dashboard/routers/wellness.py  the web pages and their /api/wellness* twins

Timing (the core rule). Every metric has a readiness class saying when a day's value is complete:
  overnight  sleep, HRV, resting HR, fatigue: stored on the WAKE date, final once the watch syncs
  snapshot   COROS recovery %, read live and stamped with the day it was read
  daytime    steps, exercise, stress, training load: only complete once the day is over
  evening    the day rating, given after the evening nudge (often backfilled the next day)
Each point carries a status: final, partial (today, still accumulating), pending (expected but not synced
yet, i.e. after the newest stored day) or missing (a real gap). Averages and baselines use final days only,
and a pending day is never shown as zero.

Every function takes an optional `db_path` and an optional `now` (or `today`) so tests can pin time.
Failures raise WellnessError with a CLI-style code: 1 = not found, 2 = bad input.
"""

from __future__ import annotations

import sqlite3
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import dayrating  # noqa: E402
import habits  # noqa: E402
import insights  # noqa: E402

RANGES = (30, 90, 180)
HEATMAP_DAYS = 182
BASELINE_DAYS = 30          # personal baseline window for vitals and bands
MIN_BASELINE = 5            # final days needed before a baseline is shown
ROLL_DAYS, ROLL_MIN = 7, 3  # rolling average window and the final days it needs
SLEEP_TARGET_HOURS = 8.0
RATING_NUDGE = (20, 30)     # the dayrating-nightly-nudge task's local time
RECOVERY_ZONES = (67, 34)   # >= 67 green, >= 34 yellow, else red (Whoop's bands)
LOAD_RATIO_ZONES = [(0, 0.8, "low", "Detraining"), (0.8, 1.5, "ok", "Optimal"), (1.5, 3, "high", "Overreaching")]
# How old each source's newest write may be before the page calls it stale (hours).
STALE_HOURS = {"coros_health": 3, "weather": 3, "habits": 3, "coros_wellness": 24 * 8, "calendar_load": 24 * 8}
SOURCE_LABELS = {"coros_health": "COROS sleep & activity", "coros_wellness": "COROS HRV & load",
                 "weather": "Weather", "habits": "Habits", "calendar_load": "Calendar"}

# key -> metric definition. `better`: "up" when higher is better, "down" when lower is, None when neutral.
METRICS: dict[str, dict] = {}


def _m(key, metric, label, unit, cls, better=None, digits=0, group="body"):
    METRICS[key] = {"key": key, "metric": metric, "label": label, "unit": unit, "class": cls,
                    "better": better, "digits": digits, "group": group}


_m("sleep_hours", "coros:sleep_hours", "Sleep", "h", "overnight", "up", 1, "sleep")
_m("sleep_score", "coros:sleep_score", "Sleep score", "", "overnight", "up", 0, "sleep")
_m("deep_pct", "coros:deep_pct", "Deep", "%", "overnight", "up", 0, "sleep")
_m("rem_pct", "coros:rem_pct", "REM", "%", "overnight", "up", 0, "sleep")
_m("light_pct", "coros:light_pct", "Light", "%", "overnight", None, 0, "sleep")
_m("awake_min", "coros:awake_min", "Awake", "min", "overnight", "down", 0, "sleep")
_m("wakeups", "coros:wakeups", "Wake-ups", "", "overnight", "down", 0, "sleep")
_m("bedtime", "coros:bedtime_after_noon", "Bedtime", "clock", "overnight", None, 2, "sleep")
_m("wake_time", "coros:wake_hour", "Wake time", "clock", "overnight", None, 2, "sleep")
_m("sleep_hr", "coros:sleep_hr_avg", "Sleeping HR", "bpm", "overnight", "down", 0, "heart")
_m("hrv", "coros:hrv_ms", "HRV", "ms", "overnight", "up", 0, "heart")
_m("hrv_base_low", "coros:hrv_base_low", "HRV normal (low)", "ms", "overnight", None, 0, "heart")
_m("hrv_base_high", "coros:hrv_base_high", "HRV normal (high)", "ms", "overnight", None, 0, "heart")
_m("resting_hr", "coros:resting_hr", "Resting HR", "bpm", "overnight", "down", 0, "heart")
_m("fatigue", "coros:fatigue", "Fatigue", "", "overnight", "down", 0, "load")
_m("recovery", "coros:recovery_pct", "Recovery", "%", "snapshot", "up", 0, "load")
_m("steps", "coros:steps", "Steps", "", "daytime", "up", 0, "activity")
_m("exercise", "coros:exercise_min", "Exercise", "min", "daytime", "up", 0, "activity")
_m("stress", "coros:stress_avg", "Stress", "", "daytime", "down", 0, "activity")
_m("load", "coros:training_load", "Training load", "", "daytime", None, 0, "load")
_m("load_7d", "coros:load_7d", "7-day load", "", "daytime", None, 0, "load")
_m("ati", "coros:ati", "Acute load (ATI)", "", "daytime", None, 0, "load")
_m("cti", "coros:cti", "Fitness (CTI)", "", "daytime", "up", 0, "load")
_m("load_ratio", "coros:load_ratio", "Load ratio", "", "daytime", None, 2, "load")
_m("load_rec_min", "coros:load_rec_min", "Recommended 7-day load (min)", "", "daytime", None, 0, "load")
_m("load_rec_max", "coros:load_rec_max", "Recommended 7-day load (max)", "", "daytime", None, 0, "load")
METRICS["rating"] = {"key": "rating", "metric": None, "label": "Day rating", "unit": "/10", "class": "evening",
                     "better": "up", "digits": 0, "group": "mood"}

# A 0 here means COROS had nothing to score (e.g. a sleep score of 0), not a real reading.
ZERO_IS_MISSING = {"sleep_score", "sleep_hours", "hrv", "resting_hr", "sleep_hr"}

# Targets the Drivers view can explain, and metrics too close to each target to count as a "driver".
DRIVER_TARGETS = {"rating": [], "hrv": ["coros:hrv_ms", "coros:hrv_vs_baseline"],
                  "sleep_score": ["coros:sleep_score", "coros:sleep_hours", "coros:deep_pct", "coros:light_pct",
                                  "coros:rem_pct", "coros:awake_min", "coros:wakeups"],
                  "resting_hr": ["coros:resting_hr", "coros:sleep_hr_avg"]}
NICE = {"weather:high_f": "High temp", "weather:low_f": "Low temp", "weather:precip_in": "Rain",
        "weather:snow_in": "Snow", "weather:wind_mph": "Wind", "weather:sunshine_hours": "Sunshine",
        "weather:cloud_pct": "Cloud cover", "weather:rainy_day": "Rainy day", "day:weekend": "Weekend",
        "calendar:events": "Calendar events", "calendar:hours_booked": "Hours booked",
        "calendar:caroline_work": "Caroline working", "calendar:travel": "Travel",
        "calendar:kid_activity": "Kid activity", "habit:total_done": "Habits done",
        "coros:hrv_vs_baseline": "HRV vs baseline", "coros:wellness_hr": "Spot-check HR",
        "coros:wellness_hrv": "Spot-check HRV", "coros:wellness_stress": "Spot-check stress",
        "coros:wellness_resp": "Breathing rate", "coros:wellness_spo2": "Blood oxygen",
        "coros:recovery_hours_left": "Recovery hours left"}


class WellnessError(Exception):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.message = message
        self.code = code


def connect(db_path=None) -> sqlite3.Connection:
    return dayrating.connect(db_path)


def _now(now: datetime | None) -> datetime:
    return now or datetime.now()


def _iso(d: date) -> str:
    return d.isoformat()


def _days(end: date, n: int) -> list[str]:
    return [_iso(end - timedelta(days=i)) for i in range(n - 1, -1, -1)]


def label_for(metric: str) -> str:
    """Human label for an insight_daily metric name (or a '(previous day)' variant of one)."""
    base, lag = (metric[:-15], " (day before)") if metric.endswith(" (previous day)") else (metric, "")
    for m in METRICS.values():
        if m["metric"] == base:
            return m["label"] + lag
    if base in NICE:
        return NICE[base] + lag
    group, _, name = base.partition(":")
    if group == "habit":
        return f"{name} (habit){lag}"
    if group == "day":
        return name + lag
    return name.replace("_", " ").capitalize() + lag


def _metric(key: str) -> dict:
    if key not in METRICS:
        raise WellnessError(f"unknown metric {key!r}; choose one of: {', '.join(sorted(METRICS))}")
    return METRICS[key]


def fmt(key: str, value) -> str:
    """A value formatted for display ('7.2 h', '11:05 PM', '48 ms')."""
    if value is None:
        return "—"
    m = METRICS.get(key, {"unit": "", "digits": 1})
    if m["unit"] == "clock":
        v = (value + (12 if key == "bedtime" else 0)) % 24
        h, mi = int(v), int(round((v - int(v)) * 60))
        if mi == 60:
            h, mi = (h + 1) % 24, 0
        return f"{(h % 12) or 12}:{mi:02d} {'AM' if h < 12 else 'PM'}"
    text = f"{value:,.{m['digits']}f}"
    if m["unit"] in ("", "/10"):
        return text + m["unit"]
    return f"{text}{'' if m['unit'] == '%' else ' '}{m['unit']}"


# ---------------------------------------------------------------------------
# reading series with timing status
# ---------------------------------------------------------------------------


def _raw(conn: sqlite3.Connection, key: str, since: str, until: str) -> dict[str, float]:
    if key == "rating":
        rows = conn.execute("SELECT rating_date d, score v FROM day_ratings WHERE rating_date BETWEEN ? AND ?",
                            (since, until)).fetchall()
    else:
        rows = conn.execute("SELECT day d, value v FROM insight_daily WHERE metric=? AND day BETWEEN ? AND ?",
                            (METRICS[key]["metric"], since, until)).fetchall()
    return {r["d"]: float(r["v"]) for r in rows if not (r["v"] == 0 and key in ZERO_IS_MISSING)}


def _newest(conn: sqlite3.Connection, key: str) -> str | None:
    if key == "rating":
        return conn.execute("SELECT MAX(rating_date) FROM day_ratings").fetchone()[0]
    return conn.execute("SELECT MAX(day) FROM insight_daily WHERE metric=?", (METRICS[key]["metric"],)).fetchone()[0]


def point_status(key: str, day: str, has_value: bool, today: str, newest: str | None) -> str:
    """final | partial | pending | missing for one metric on one day (see the module docstring)."""
    cls = METRICS[key]["class"]
    if day > today:
        return "pending"
    if day == today and cls == "daytime":
        return "partial" if has_value else "pending"
    if has_value:
        return "final"
    if day == today:  # overnight not synced yet, rating not given yet, snapshot not read yet
        return "pending"
    if cls == "evening":
        return "missing"
    return "pending" if newest is None or day > newest else "missing"


def series(key: str, days: int, *, db_path=None, today: date | None = None, conn=None) -> dict:
    """One metric over the last `days` days (today included): points with status, a rolling 7-day average and
    a summary that compares this window's average with the one before it. Uses final days only."""
    _metric(key)
    today = today or datetime.now().date()
    own = conn is None
    conn = conn or connect(db_path)
    try:
        span = _days(today, days * 2)
        raw = _raw(conn, key, span[0], span[-1])
        newest = _newest(conn, key)
    finally:
        if own:
            conn.close()
    t = _iso(today)
    window = span[days:]
    points = []
    for d in window:
        v = raw.get(d)
        st = point_status(key, d, v is not None, t, newest)
        points.append({"date": d, "value": v, "status": st})
    final = {p["date"]: p["value"] for p in points if p["status"] == "final"}
    before = [raw[d] for d in span[:days] if d in raw and d < t]
    return {"key": key, "label": METRICS[key]["label"], "unit": METRICS[key]["unit"],
            "better": METRICS[key]["better"], "points": points,
            "avg7": rolling(final, window), "newest": newest,
            "summary": _summary(list(final.values()), before)}


def rolling(final: dict[str, float], window: list[str], n: int = ROLL_DAYS, need: int = ROLL_MIN) -> list[dict]:
    """Trailing n-day average of final values, None where fewer than `need` final days fall in the window."""
    out = []
    for i, d in enumerate(window):
        vals = [final[x] for x in window[max(0, i - n + 1):i + 1] if x in final]
        out.append({"date": d, "value": round(sum(vals) / len(vals), 2) if len(vals) >= need else None})
    return out


def _summary(vals: list[float], before: list[float]) -> dict:
    if not vals:
        return {"days": 0, "avg": None, "min": None, "max": None, "prev_avg": None, "delta": None}
    avg = sum(vals) / len(vals)
    prev = sum(before) / len(before) if before else None
    return {"days": len(vals), "avg": round(avg, 2), "min": min(vals), "max": max(vals),
            "prev_avg": round(prev, 2) if prev is not None else None,
            "delta": round(avg - prev, 2) if prev is not None else None}


def baseline(vals: list[float]) -> dict | None:
    """Personal normal range: mean +/- 1 SD of final values, or None with too few days."""
    if len(vals) < MIN_BASELINE:
        return None
    mean, sd = statistics.fmean(vals), statistics.pstdev(vals)
    return {"mean": round(mean, 2), "sd": round(sd, 2), "low": round(mean - sd, 2), "high": round(mean + sd, 2),
            "days": len(vals)}


def rolling_band(final: dict[str, float], window: list[str], n: int = BASELINE_DAYS) -> list[dict]:
    """For each day, the baseline of the final values in the n days BEFORE it (so a day is judged against
    its own past, not against itself)."""
    out = []
    for d in window:
        lo = _iso(date.fromisoformat(d) - timedelta(days=n))
        b = baseline([v for x, v in final.items() if lo <= x < d])
        out.append({"date": d, "low": b["low"] if b else None, "high": b["high"] if b else None})
    return out


# ---------------------------------------------------------------------------
# readiness
# ---------------------------------------------------------------------------


def zone(score: float | None) -> str | None:
    if score is None:
        return None
    return "good" if score >= RECOVERY_ZONES[0] else "mid" if score >= RECOVERY_ZONES[1] else "bad"


def readiness_score(hrv_z: float | None, rhr_z: float | None, sleep_score: float | None) -> int | None:
    """Our fallback readiness (0-100) when COROS recovery isn't fresh. HRV above its baseline and resting HR
    below its baseline both help; each z-score maps to 50 +/- 25 per SD. Weights: HRV 50%, sleep score 30%,
    resting HR 20%, re-weighted over whichever parts are available."""
    parts = []
    if hrv_z is not None:
        parts.append((0.5, 50 + 25 * hrv_z))
    if sleep_score is not None:
        parts.append((0.3, sleep_score))
    if rhr_z is not None:
        parts.append((0.2, 50 - 25 * rhr_z))
    if not parts:
        return None
    total = sum(w for w, _ in parts)
    return int(round(max(0, min(100, sum(w * v for w, v in parts) / total))))


def _vital(conn, key: str, today: date, within: int = 3) -> dict:
    """Latest value of a metric (final days only) against its baseline over the BASELINE_DAYS before it."""
    m = METRICS[key]
    t = _iso(today)
    raw = _raw(conn, key, _iso(today - timedelta(days=BASELINE_DAYS + within)), t)
    newest = _newest(conn, key)
    if m["class"] == "daytime":  # today's partial value doesn't count
        raw = {d: v for d, v in raw.items() if d < t}
    recent = [d for d in sorted(raw) if d >= _iso(today - timedelta(days=within))]
    expected = _iso(today - timedelta(days=1)) if m["class"] == "daytime" else t
    out = {"key": key, "label": m["label"], "unit": m["unit"], "better": m["better"],
           "status": point_status(key, expected, expected in raw, t, newest)}
    if not recent:
        out.update(value=None, day=None, display="—", newest=newest)
        return out
    day = recent[-1]
    v = raw[day]
    lo = _iso(date.fromisoformat(day) - timedelta(days=BASELINE_DAYS))
    b = baseline([x for d, x in raw.items() if lo <= d < day])
    yday = _iso(today - timedelta(days=1))
    when = ("Last night" if day == t else "Yesterday, full day" if m["class"] == "daytime" and day == yday
            else f"Last synced: {day}")
    out.update(value=v, day=day, display=fmt(key, v), baseline=b, newest=newest, when=when,
               spark=[{"date": d, "value": raw.get(d)} for d in _days(today, 14)])
    if b:
        z = (v - b["mean"]) / b["sd"] if b["sd"] else 0.0
        out["z"] = round(z, 2)
        out["delta"] = round(v - b["mean"], 2)
        out["range"] = "above" if v > b["high"] else "below" if v < b["low"] else "normal"
        good = {"up": out["range"] == "above", "down": out["range"] == "below"}.get(m["better"], False)
        bad = {"up": out["range"] == "below", "down": out["range"] == "above"}.get(m["better"], False)
        out["verdict"] = "good" if good else "bad" if bad else "normal"
    return out


def _freshness(now: datetime, db_path) -> list[dict]:
    out = []
    for src, at in sorted(insights.freshness(db_path=db_path).items()):
        if src not in SOURCE_LABELS:
            continue
        age = (now - datetime.fromisoformat(at)).total_seconds() / 3600
        out.append({"source": src, "label": SOURCE_LABELS[src], "at": at, "age_hours": round(age, 1),
                    "stale": age > STALE_HOURS.get(src, 24)})
    return out


def _mood(now: datetime, db_path) -> dict:
    """The day rating, timing-aware: it's given in the evening, so before then show yesterday's."""
    today = now.date()
    st = dayrating.status(db_path=db_path, today=today)
    yday = dayrating.get_rating((today - timedelta(days=1)).isoformat(), db_path=db_path, today=today)
    nudge_at = now.replace(hour=RATING_NUDGE[0], minute=RATING_NUDGE[1], second=0, microsecond=0)
    if st["rated_today"]:
        shown, when, today_state = st["today"], "Today", "rated"
    else:
        shown, when = yday, "Yesterday"
        today_state = "due" if now >= nudge_at else "later"
    recent = dayrating.list_ratings(days=14, db_path=db_path, today=today)
    return {"shown": shown, "shown_when": when if shown else None, "today_state": today_state,
            "nudge_label": fmt("wake_time", RATING_NUDGE[0] + RATING_NUDGE[1] / 60),
            "yesterday_unrated": st["yesterday_unrated"], "avg_7": st["avg_7"], "avg_30": st["avg_30"],
            "streak": st["logging_streak"],
            "spark": [{"date": d, "value": next((r["score"] for r in recent if r["date"] == d), None)}
                      for d in _days(today, 14)]}


def today_view(*, now: datetime | None = None, db_path=None) -> dict:
    """Last night, today so far and yesterday complete: readiness rings, last night's sleep, vitals against
    baseline, the day rating, today's habits and how fresh each source is."""
    now = _now(now)
    today, t = now.date(), _iso(now.date())
    conn = connect(db_path)
    try:
        vitals = {k: _vital(conn, k, today) for k in ("hrv", "resting_hr", "sleep_hr", "stress", "fatigue")}
        sleep_keys = ("sleep_hours", "sleep_score", "deep_pct", "rem_pct", "light_pct", "awake_min", "wakeups",
                      "bedtime", "wake_time")
        night = {k: _raw(conn, k, _iso(today - timedelta(days=3)), t) for k in sleep_keys}
        hours_hist = _raw(conn, "sleep_hours", _iso(today - timedelta(days=BASELINE_DAYS)), t)
        rec = _raw(conn, "recovery", t, t)
        load7 = _raw(conn, "load_7d", _iso(today - timedelta(days=7)), t)
        rec_lo = _raw(conn, "load_rec_min", _iso(today - timedelta(days=14)), t)
        rec_hi = _raw(conn, "load_rec_max", _iso(today - timedelta(days=14)), t)
    finally:
        conn.close()

    # Last night is keyed to today's (wake) date; until the watch syncs, fall back to the newest night.
    sleep_day = t if t in night["sleep_hours"] or t in night["sleep_score"] else max(
        set(night["sleep_hours"]) | set(night["sleep_score"]), default=None)
    sleep = {"day": sleep_day, "status": "final" if sleep_day == t else "pending",
             "values": {k: night[k].get(sleep_day) for k in sleep_keys} if sleep_day else {}}
    v = sleep["values"]
    if v and None not in (v.get("deep_pct"), v.get("light_pct")):  # awake is the remainder of time in bed
        sleep["awake_pct"] = round(max(0.0, 100 - v["deep_pct"] - (v.get("rem_pct") or 0) - v["light_pct"]), 1)
    sleep["display"] = {k: fmt(k, x) for k, x in v.items()}
    prior = [v for d, v in hours_hist.items() if d != sleep_day]
    sleep["avg_hours_30"] = round(sum(prior) / len(prior), 1) if prior else None
    sleep["target_hours"] = SLEEP_TARGET_HOURS

    if t in rec:
        recovery = {"value": int(round(rec[t])), "source": "coros", "note": "COROS recovery, read today"}
    else:
        score = readiness_score(vitals["hrv"].get("z") if vitals["hrv"].get("day") == t else None,
                                vitals["resting_hr"].get("z") if vitals["resting_hr"].get("day") == t else None,
                                sleep["values"].get("sleep_score") if sleep["status"] == "final" else None)
        recovery = {"value": score, "source": "estimate" if score is not None else None,
                    "note": "Estimated from last night's HRV, resting HR and sleep score against your baseline"
                    if score is not None else "Waiting for last night's data to sync"}
    recovery["zone"] = zone(recovery["value"])

    strain = {"value": None, "low": None, "high": None, "zone": None, "day": None}
    if load7:
        d = max(load7)
        lo = rec_lo[max(rec_lo)] if rec_lo else None
        hi = rec_hi[max(rec_hi)] if rec_hi else None
        v = load7[d]
        strain.update(value=v, day=d, low=lo, high=hi,
                      zone=None if lo is None or hi is None else "low" if v < lo else "high" if v > hi else "ok")

    goals = [h for h in habits.list_status(db_path=db_path, today=today) if h["has_goal"]]
    out_of_range = [v["label"] for v in vitals.values() if v.get("verdict") == "bad"]
    return {
        "now": now.strftime("%Y-%m-%dT%H:%M:%S"), "today": t,
        "rings": {"recovery": recovery,
                  "sleep": {"value": sleep["values"].get("sleep_score"), "hours": sleep["values"].get("sleep_hours"),
                            "status": sleep["status"], "day": sleep_day,
                            "zone": zone(sleep["values"].get("sleep_score"))},
                  "strain": strain},
        "sleep": sleep, "vitals": list(vitals.values()), "out_of_range": out_of_range,
        "mood": _mood(now, db_path),
        "habits": [{"name": h["name"], "status": h["period_status"], "done": h["done_this_period"],
                    "target": h["target_count"], "frequency": h["frequency"], "streak": h["current_streak"]}
                   for h in goals],
        "freshness": _freshness(now, db_path),
    }


# ---------------------------------------------------------------------------
# trends
# ---------------------------------------------------------------------------


def _check_range(days) -> int:
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise WellnessError(f"range must be one of {', '.join(map(str, RANGES))}") from None
    if days not in RANGES:
        raise WellnessError(f"range must be one of {', '.join(map(str, RANGES))}")
    return days


def _check_days(days) -> int:
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise WellnessError("days must be a whole number 7-366") from None
    if not 7 <= days <= 366:
        raise WellnessError("days must be between 7 and 366")
    return days


def _weekly(points: list[dict], how: str) -> list[dict]:
    """Monday-start weekly roll-up of final/partial points: 'avg' per day or 'sum'."""
    weeks: dict[str, list[float]] = {}
    for p in points:
        if p["value"] is None:
            continue
        d = date.fromisoformat(p["date"])
        weeks.setdefault(_iso(d - timedelta(days=d.weekday())), []).append(p["value"])
    return [{"week": w, "days": len(v), "value": round(sum(v) / len(v) if how == "avg" else sum(v), 1)}
            for w, v in sorted(weeks.items())]


def trends(range_days=90, *, now: datetime | None = None, db_path=None) -> dict:
    """Every trend chart's data for the last `range_days` days (30, 90 or 180)."""
    days = _check_range(range_days)
    now = _now(now)
    today = now.date()
    conn = connect(db_path)
    try:
        s = {k: series(k, days, today=today, conn=conn) for k in METRICS if k != "recovery"}
        # Bands look back BASELINE_DAYS before the window starts.
        wide = {k: _raw(conn, k, _iso(today - timedelta(days=days + BASELINE_DAYS)), _iso(today))
                for k in ("hrv", "resting_hr")}
    finally:
        conn.close()
    window = [p["date"] for p in s["sleep_hours"]["points"]]
    t = _iso(today)

    def final(k):
        return {d: v for d, v in wide[k].items() if d < t or METRICS[k]["class"] != "daytime"}

    # HRV band: COROS's own normal range when stored, else our rolling baseline.
    coros_band = {p["date"]: (p["value"], h["value"]) for p, h in
                  zip(s["hrv_base_low"]["points"], s["hrv_base_high"]["points"]) if p["value"] and h["value"]}
    own_band = rolling_band(final("hrv"), window)
    hrv_band = [{"date": b["date"], "low": coros_band[b["date"]][0], "high": coros_band[b["date"]][1],
                 "source": "coros"} if b["date"] in coros_band else {**b, "source": "rolling"} for b in own_band]
    for p in s["hrv"]["points"]:
        b = next(x for x in hrv_band if x["date"] == p["date"])
        if p["value"] is not None and b["low"] is not None:
            p["band"] = "above" if p["value"] > b["high"] else "below" if p["value"] < b["low"] else "normal"

    bed_final = [p["value"] for p in s["bedtime"]["points"][-14:] if p["status"] == "final"]
    consistency = round(statistics.pstdev(bed_final) * 60) if len(bed_final) >= MIN_BASELINE else None
    weekday: dict[int, list[float]] = {}
    for p in s["rating"]["points"]:
        if p["status"] == "final":
            weekday.setdefault(date.fromisoformat(p["date"]).weekday(), []).append(p["value"])
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    stages = []
    for parts in zip(*(s[k]["points"] for k in ("deep_pct", "rem_pct", "light_pct"))):
        vals = [p["value"] for p in parts]
        if None not in vals:  # awake is the remainder of time in bed
            stages.append({"date": parts[0]["date"], "deep": vals[0], "rem": vals[1], "light": vals[2],
                           "awake": round(max(0.0, 100 - sum(vals)), 1)})
    return {
        "range": days, "ranges": list(RANGES), "start": window[0], "end": window[-1], "dates": window,
        "today": t, "series": s,
        "sleep": {"target_hours": SLEEP_TARGET_HOURS, "bedtime_sd_min": consistency,
                  "avg_bed": fmt("bedtime", statistics.fmean(bed_final)) if bed_final else None},
        "stages": stages,
        "hrv_band": hrv_band,
        "rhr_band": rolling_band(final("resting_hr"), window),
        "load_ratio_zones": [{"low": a, "high": b, "zone": z, "label": lab} for a, b, z, lab in LOAD_RATIO_ZONES],
        "activity_weeks": {"steps": _weekly(s["steps"]["points"], "avg"),
                           "exercise": _weekly(s["exercise"]["points"], "sum")},
        "rating_by_weekday": [{"day": names[i], "avg": round(sum(weekday[i]) / len(weekday[i]), 1) if i in weekday
                               else None, "n": len(weekday.get(i, []))} for i in range(7)],
    }


def heatmap(metric="rating", *, days: int = HEATMAP_DAYS, now: datetime | None = None, db_path=None) -> dict:
    """GitHub-style calendar of one metric: Monday-start week columns, each day bucketed into 5 levels by the
    window's own quintiles (level 0 = no value)."""
    m = _metric(metric)
    now = _now(now)
    today = now.date()
    start = today - timedelta(days=days - 1)
    start -= timedelta(days=start.weekday())
    s = series(metric, (today - start).days + 1, today=today, db_path=db_path)
    vals = sorted(p["value"] for p in s["points"] if p["status"] == "final")
    cuts = [vals[int(len(vals) * q / 5)] for q in range(1, 5)] if len(vals) >= 5 else []
    flip = m["better"] == "down"

    def level(v):
        if v is None:
            return 0
        lv = 1 + sum(v >= c for c in cuts) if cuts else 3
        return 6 - lv if flip else lv

    weeks, col = [], []
    for p in s["points"]:
        col.append({**p, "level": level(p["value"]) if p["status"] in ("final", "partial") else 0,
                    "display": fmt(metric, p["value"])})
        if len(col) == 7:
            weeks.append(col)
            col = []
    if col:
        weeks.append(col)
    return {"metric": metric, "label": m["label"], "weeks": weeks, "better": m["better"],
            "choices": [{"key": k, "label": METRICS[k]["label"]} for k in HEATMAP_CHOICES]}


HEATMAP_CHOICES = ["rating", "sleep_score", "sleep_hours", "hrv", "resting_hr", "steps", "load", "stress"]


# ---------------------------------------------------------------------------
# drivers and scatter
# ---------------------------------------------------------------------------


def drivers(target="rating", *, days: int = 90, now: datetime | None = None, db_path=None) -> dict:
    """What moves a target (the day rating, HRV, sleep score or resting HR): the insights engine's findings,
    labelled for people, with the target's own components left out."""
    if target not in DRIVER_TARGETS:
        raise WellnessError(f"target must be one of: {', '.join(DRIVER_TARGETS)}")
    days = _check_days(days)
    today = _now(now).date()
    if target == "rating":
        stats = insights.analyze(days=days, db_path=db_path, today=today)
        n_target = stats["ratings"]["count"]
    else:
        stats = insights.analyze_against(METRICS[target]["metric"], days=days, db_path=db_path, today=today,
                                         exclude=DRIVER_TARGETS[target])
        n_target = stats["target_days"]
    unit = "points" if target == "rating" else METRICS[target]["unit"]
    findings = []
    for f in stats["findings"]:
        findings.append({"metric": f["metric"], "label": label_for(f["metric"]), "lag_days": f["lag_days"],
                         "kind": f["kind"], "correlation": f["correlation"], "effect": f.get("effect"),
                         "avg_with": f.get("avg_rating_with"), "avg_without": f.get("avg_rating_without"),
                         "n": f["n"], "p_value": f["p_value"], "confidence": f["confidence"]})
    return {"target": target, "label": METRICS[target]["label"], "unit": unit, "days": days,
            "target_days": n_target, "enough_data": n_target >= insights.MIN_DAYS,
            "min_days_needed": insights.MIN_DAYS, "findings": findings,
            "targets": [{"key": k, "label": METRICS[k]["label"]} for k in DRIVER_TARGETS],
            "caveats": stats["caveats"],
            "report": insights.latest_report(db_path=db_path) if target == "rating" else None}


SCATTER_CHOICES = ["rating", "sleep_hours", "sleep_score", "hrv", "resting_hr", "sleep_hr", "deep_pct", "rem_pct",
                   "steps", "exercise", "stress", "load", "fatigue", "bedtime"]


def scatter(x="sleep_hours", y="rating", *, days: int = 90, now: datetime | None = None, db_path=None) -> dict:
    """Paired final values of two metrics on the same day, with Spearman r and a least-squares line."""
    for k in (x, y):
        _metric(k)
    if x == y:
        raise WellnessError("pick two different metrics")
    days = _check_days(days)
    today = _now(now).date()
    conn = connect(db_path)
    try:
        sx, sy = series(x, days, today=today, conn=conn), series(y, days, today=today, conn=conn)
    finally:
        conn.close()
    fy = {p["date"]: p["value"] for p in sy["points"] if p["status"] == "final"}
    pts = [{"date": p["date"], "x": p["value"], "y": fy[p["date"]]}
           for p in sx["points"] if p["status"] == "final" and p["date"] in fy]
    xs, ys = [p["x"] for p in pts], [p["y"] for p in pts]
    fit = None
    if len(pts) >= 3 and len(set(xs)) > 1:
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        slope = sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / sum((a - mx) ** 2 for a in xs)
        fit = {"slope": round(slope, 4), "intercept": round(my - slope * mx, 4)}
    r = insights.spearman(xs, ys) if len(pts) >= 3 else None
    return {"x": {"key": x, "label": METRICS[x]["label"], "unit": METRICS[x]["unit"]},
            "y": {"key": y, "label": METRICS[y]["label"], "unit": METRICS[y]["unit"]},
            "days": days, "n": len(pts), "points": pts, "r": round(r, 3) if r is not None else None, "fit": fit,
            "choices": [{"key": k, "label": METRICS[k]["label"]} for k in SCATTER_CHOICES]}
