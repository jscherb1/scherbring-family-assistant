"""Training: the single shared service for running volume and heart-rate-zone time from COROS.

Zero-dependency (Python stdlib only), same SQLite file as the rest of the assistant
(state/agent_results.db, schema in state/schema.sql: activities, activity_zone_time, training_config).
Both front ends are thin wrappers over this module:
  - scripts/training_store.py       the CLI the training subagent runs
  - dashboard/data/training.py      the dashboard section (calls these functions directly)

Every function takes an optional `db_path` (default state/agent_results.db) and, where dates matter, an
optional `today` (date) so tests can pin the clock. Failures raise TrainingError with a CLI-style code:
1 = not found, 2 = bad input, 3 = COROS sync failure. All return values are plain JSON-serialisable
dicts/lists. Dates in and out are ISO strings YYYY-MM-DD (date objects are accepted as input).

PUBLIC API (all keyword args after the first are optional)
  sports_available(db_path=None) -> [{group, label, count, default}]            filter chips
  volume(metric="distance", bucket="week", sports=None, start=None, end=None, db_path=None, today=None)
      -> {metric, bucket, start, end, sports, rows:[{bucket_start, bucket_end, distance_m, distance_mi,
          time_s, runs, rolling_avg_distance_mi, rolling_avg_time_s, rolling_avg}], total:{distance_m,
          distance_mi, time_s, runs}, avg_per_week:{distance_mi, time_s}}
      Monday-start weeks, empty weeks zero-filled; `rolling_avg` is the 4-week average of the chosen metric
      (distance in miles or time in seconds); start is snapped back to its Monday. Default: last 12 weeks.
  zone_time(bucket="total", sports=None, start=None, end=None, ...)
      -> {bucket, start, end, sports, rows:[{bucket_start, label, seconds:[z0..z5], total_s, activities}]}
      bucket: total (one row) | day | week | month. Default range: the last 30 days ending today inclusive.
  zone_share(...same...) -> rows:[{bucket_start, label, percent:[z0..z5] summing to 100, total_s}]
      percent is None for a bucket with no zone data.
  easy_share(bucket="week", ...) -> {bucket, ..., rows:[{bucket_start, label, easy_pct, z1_z2_pct, total_s}],
      total:{easy_pct, z1_z2_pct, total_s}}
      "easy" = zones 0-2, i.e. below Z3. Zone 0 is the sub-Z1 band (< 137 bpm for the current account,
      warm-up / very easy) and counts as easy. z1_z2_pct counts zones 1-2 only (denominator still all time).
  longest_run_per_week(sports=None, start=None, end=None, ...)
      -> {start, end, sports, rows:[{bucket_start, longest_distance, longest_time}]} where each is None or
         {coros_id, name, day, distance_mi, distance_m, moving_s}. Default: last 12 weeks.
  summary(sports=None, start=None, end=None, ...)
      -> {start, end, sports, days, activities, total_distance_m, total_distance_mi, total_time_s,
          avg_weekly_distance_mi, avg_weekly_time_s, zone_seconds:[6], zone_total_s, z3_plus_pct, easy_pct}
      Default range: last 30 days. Averages are total / (days/7). Percentages are None without zone data.
  activities(sports=None, start=None, end=None, limit=50, ...)
      -> [{coros_id, name, day, start_local, sport_type, sport_name, group, is_indoor, distance_m,
           distance_mi, moving_s, pace_s_per_mi, avg_hr, max_hr, training_load, zone_seconds | None}]
      newest first.
  zone_definitions(db_path=None) -> {lthr, max_hr, zones:[{zone, label, low, high}], updated_at}
  get_config(db_path=None) -> {default_sports, lthr, max_hr, ...}; set_config(key, value, db_path=None)
  sync(db_path=None, *, all_sports=False, full=False, today=None, fetch_list_pages=None,
       fetch_detail=None, fetch_account=None) -> {window_start, listed, inserted, updated,
       detail_attempted, detail_fetched, detail_failed, errors:[str], partial:bool}
      The three fetch_* callables are the ONLY network code (see "COROS network layer" below) and are
      injectable: fetch_list_pages(start: date, end: date) -> [list row], fetch_detail(coros_id, sport_type)
      -> detail `data` dict, fetch_account() -> account `data` dict.

SPORTS: a list (or comma string) of group names and/or COROS sport ids. Groups: running (100 outdoor,
101 treadmill; rows named "...walk..." excluded), walking (100/101 rows named "walk"), cycling (200, 201),
hiking (104), strength (402), other (everything else), all. Default (sports=None) is the config key
default_sports, initially "running". A raw sport id matches that sportType exactly.

ZONE SECONDS RULE: COROS zone seconds can sum to more than moving time on some outdoor runs while
`percent` stays consistent. Raw seconds and percent are both stored; for analysis an activity's zone
seconds are the raw seconds when they sum to within 2% of moving_s, otherwise percent/100 * moving_s.

SYNC: idempotent upsert by COROS label id; lists from (latest stored day - 14 days) or all history with
full=True / empty db; fetches detail (zones) only for activities in the selected sports that have none yet
(default sports from config, every sport with all_sports=True); per-activity failures are collected in
`errors` and do not stop the run (partial=True). The CLI exits 3 when partial.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "state" / "agent_results.db"
SCHEMA_PATH = REPO_ROOT / "state" / "schema.sql"

METERS_PER_MILE = 1609.344
BUCKETS = ("total", "day", "week", "month")
METRICS = ("distance", "time")
SPORT_NAMES = {100: "Run", 101: "Treadmill Run", 104: "Hike", 200: "Road Bike", 201: "Indoor Bike",
               300: "Pool Swim", 400: "Gym Cardio", 402: "Strength", 500: "Ski", 701: "Rower",
               904: "Yoga", 9904: "Sauna"}
INDOOR_TYPES = {101, 201, 300, 400, 402, 701}
GROUPS = {"running": "Running", "walking": "Walking", "cycling": "Cycling", "hiking": "Hiking",
          "strength": "Strength", "other": "Other"}
SPORT_GROUPS = {"running": [100, 101], "cycling": [200, 201], "strength": [402], "hiking": [104]}
ZONE_LABELS = ["Below Z1", "Z1", "Z2", "Z3", "Z4", "Z5"]
CONFIG_DEFAULTS = {"default_sports": "running", "lthr": "171", "max_hr": "193"}
CONFIG_KEYS = ("default_sports", "lthr", "max_hr", "lthr_zone", "hr_zone_bounds")
ROLLING_WEEKS = 4
DEFAULT_WEEKS = 12
DEFAULT_DAYS = 30
INCREMENTAL_OVERLAP_DAYS = 14
ZONE_TOLERANCE = 0.02


class TrainingError(Exception):
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
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _today(today: date | None) -> date:
    return today or datetime.now().date()


def _parse_date(value, field: str = "date") -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        raise TrainingError(f"invalid {field} {value!r}, expected YYYY-MM-DD") from None


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _check(value: str, allowed: tuple, field: str) -> str:
    if value not in allowed:
        raise TrainingError(f"invalid {field} {value!r}, expected one of: {', '.join(allowed)}")
    return value


def _range(start, end, today: date | None, default_days: int | None = None,
           default_weeks: int | None = None) -> tuple[date, date]:
    t = _today(today)
    s, e = _parse_date(start, "start"), _parse_date(end, "end")
    e = e or t
    if s is None:
        if default_weeks:
            s = _monday(e) - timedelta(weeks=default_weeks - 1)
        else:
            s = e - timedelta(days=(default_days or DEFAULT_DAYS) - 1)
    if s > e:
        raise TrainingError(f"start {s} is after end {e}")
    return s, e


def _group_of(sport_type: int, name: str | None) -> str:
    if sport_type in (100, 101) and "walk" in (name or "").lower():
        return "walking"
    for g, ids in SPORT_GROUPS.items():
        if sport_type in ids:
            return g
    return "other"


def _parse_sports(sports, db_path=None) -> tuple[set[str], set[int]]:
    """Resolve a sports selector to (group names, raw sport ids). None -> configured default."""
    if sports is None:
        sports = get_config(db_path)["default_sports"]
    if isinstance(sports, (str, int)):
        sports = str(sports).split(",")
    groups: set[str] = set()
    ids: set[int] = set()
    for item in sports:
        s = str(item).strip().lower()
        if not s:
            continue
        if s == "all":
            groups |= set(GROUPS)
        elif s in GROUPS:
            groups.add(s)
        elif s.isdigit():
            ids.add(int(s))
        else:
            raise TrainingError(f"unknown sport {item!r}; use one of {', '.join(GROUPS)}, all, or a sport id")
    if not groups and not ids:
        raise TrainingError("no sports selected")
    return groups, ids


def _sel_label(sel: tuple[set[str], set[int]]) -> list[str]:
    return sorted(sel[0]) + [str(i) for i in sorted(sel[1])]


def _matches(sel, sport_type: int, name: str | None) -> bool:
    return _group_of(sport_type, name) in sel[0] or sport_type in sel[1]


def _miles(m: float | None) -> float:
    return round((m or 0.0) / METERS_PER_MILE, 2)


def effective_zone_seconds(moving_s, zones: list[tuple[int, float, float | None]]) -> list[float]:
    """Six zone-second values (index 0..5) for one activity, applying the ZONE SECONDS RULE.

    `zones` is [(zone, seconds, percent)]. Raw seconds are used when they sum to within 2% of moving
    time; otherwise percent/100 * moving time; with no moving time or no percent, raw seconds.
    """
    raw = [0.0] * 6
    pct = [0.0] * 6
    for z, sec, p in zones:
        raw[z] = float(sec or 0)
        pct[z] = float(p or 0)
    total_raw, total_pct = sum(raw), sum(pct)
    if moving_s and moving_s > 0:
        if total_raw > 0 and abs(total_raw - moving_s) / moving_s <= ZONE_TOLERANCE:
            return raw
        if total_pct > 0:
            return [p / 100.0 * moving_s for p in pct]
    return raw


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def _put_config(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO training_config(key, value, updated_at) VALUES (?,?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                 (key, value, _now_iso()))


def get_config(db_path=None) -> dict:
    conn = connect(db_path)
    try:
        out = dict(CONFIG_DEFAULTS)
        for r in conn.execute("SELECT key, value FROM training_config"):
            out[r["key"]] = r["value"]
    finally:
        conn.close()
    return out


def set_config(key: str, value, db_path=None) -> dict:
    if key not in CONFIG_KEYS:
        raise TrainingError(f"unknown config key {key!r}; use one of {', '.join(CONFIG_KEYS)}")
    value = str(value).strip()
    if key == "default_sports":
        _parse_sports(value)
    elif key in ("lthr", "max_hr"):
        if not value.isdigit() or not 60 <= int(value) <= 250:
            raise TrainingError(f"{key} must be a whole number of bpm between 60 and 250")
    else:
        try:
            json.loads(value)
        except ValueError:
            raise TrainingError(f"{key} must be valid JSON") from None
    conn = connect(db_path)
    try:
        _put_config(conn, key, value)
        conn.commit()
    finally:
        conn.close()
    return get_config(db_path)


def _lthr_zone_bounds(raw) -> dict:
    """Zone bpm ranges from the account's current LTHR zones ({hr, index}: upper bound of zones 0..4).

    Preferred over per-activity bounds, which can reflect a different zone method (e.g. % of max HR).
    Z0 is below the first bound, Z1 starts at it, later zones start 1 bpm above the previous bound,
    and Z5 is open-ended.
    """
    try:
        uppers = {int(z["index"]): int(z["hr"]) for z in json.loads(raw or "[]")}
    except (ValueError, KeyError, TypeError):
        return {}
    if not all(i in uppers for i in range(5)):
        return {}
    out = {0: (None, uppers[0]), 1: (uppers[0], uppers[1])}
    for z in (2, 3, 4):
        out[z] = (uppers[z - 1] + 1, uppers[z])
    out[5] = (uppers[4], None)
    return out


def _detail_zone_bounds(raw) -> dict:
    try:
        return {int(b["zone"]): (b.get("low"), b.get("high")) for b in json.loads(raw or "[]")}
    except (ValueError, KeyError, TypeError):
        return {}


def zone_definitions(db_path=None) -> dict:
    conn = connect(db_path)
    try:
        cfg = dict(CONFIG_DEFAULTS)
        updated = {}
        for r in conn.execute("SELECT key, value, updated_at FROM training_config"):
            cfg[r["key"]] = r["value"]
            updated[r["key"]] = r["updated_at"]
    finally:
        conn.close()
    bounds = _lthr_zone_bounds(cfg.get("lthr_zone")) or _detail_zone_bounds(cfg.get("hr_zone_bounds"))
    zones = []
    for z in range(6):
        low, high = bounds.get(z, (None, None))
        if z == 0 and high is None:
            high = 137
        zones.append({"zone": z, "label": ZONE_LABELS[z], "low": low, "high": high})
    return {"lthr": int(cfg["lthr"]), "max_hr": int(cfg["max_hr"]), "zones": zones,
            "updated_at": max((updated.get(k) for k in ("lthr", "max_hr", "lthr_zone", "hr_zone_bounds") if updated.get(k)),
                              default=None)}


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def _load(conn, sel, start: date | None, end: date | None, with_zones: bool = False) -> list[dict]:
    sql, args = "SELECT * FROM activities WHERE 1=1", []
    if start:
        sql += " AND day >= ?"
        args.append(start.isoformat())
    if end:
        sql += " AND day <= ?"
        args.append(end.isoformat())
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY day, start_local", args)]
    rows = [r for r in rows if _matches(sel, r["sport_type"], r["name"])]
    if with_zones and rows:
        zmap: dict[str, list] = {}
        for z in conn.execute("SELECT activity_id, zone, seconds, percent FROM activity_zone_time"):
            zmap.setdefault(z["activity_id"], []).append((z["zone"], z["seconds"], z["percent"]))
        for r in rows:
            zs = zmap.get(r["id"])
            r["zone_seconds"] = effective_zone_seconds(r["moving_s"], zs) if zs else None
    return rows


def sports_available(db_path=None) -> list[dict]:
    default = _parse_sports(None, db_path)[0]
    conn = connect(db_path)
    try:
        counts: dict[str, int] = {g: 0 for g in GROUPS}
        for r in conn.execute("SELECT sport_type, name FROM activities"):
            counts[_group_of(r["sport_type"], r["name"])] += 1
    finally:
        conn.close()
    return [{"group": g, "label": GROUPS[g], "count": c, "default": g in default}
            for g, c in counts.items() if c or g == "running"]


def _week_rows(acts: list[dict], first: date, last: date) -> dict[date, list[dict]]:
    buckets: dict[date, list[dict]] = {}
    d = first
    while d <= last:
        buckets[d] = []
        d += timedelta(weeks=1)
    for a in acts:
        m = _monday(date.fromisoformat(a["day"]))
        if m in buckets:
            buckets[m].append(a)
    return buckets


def volume(metric: str = "distance", bucket: str = "week", sports=None, start=None, end=None,
           db_path=None, today: date | None = None) -> dict:
    _check(metric, METRICS, "metric")
    _check(bucket, ("week",), "bucket")
    s, e = _range(start, end, today, default_weeks=DEFAULT_WEEKS)
    sel = _parse_sports(sports, db_path)
    first, last = _monday(s), _monday(e)
    warm = first - timedelta(weeks=ROLLING_WEEKS - 1)  # history so the first rolling average is full
    conn = connect(db_path)
    try:
        acts = _load(conn, sel, warm, e)
    finally:
        conn.close()
    per = []
    for wk, items in _week_rows(acts, warm, last).items():
        per.append({"bucket_start": wk, "distance_m": sum(a["distance_m"] or 0 for a in items),
                    "time_s": sum(a["moving_s"] or 0 for a in items), "runs": len(items)})
    rows = []
    for i, p in enumerate(per):
        if p["bucket_start"] < first:
            continue
        win = per[max(0, i - ROLLING_WEEKS + 1): i + 1]
        r_mi = round(sum(w["distance_m"] for w in win) / len(win) / METERS_PER_MILE, 2)
        r_s = round(sum(w["time_s"] for w in win) / len(win))
        rows.append({"bucket_start": p["bucket_start"].isoformat(),
                     "bucket_end": (p["bucket_start"] + timedelta(days=6)).isoformat(),
                     "distance_m": round(p["distance_m"]), "distance_mi": _miles(p["distance_m"]),
                     "time_s": int(p["time_s"]), "runs": p["runs"],
                     "rolling_avg_distance_mi": r_mi, "rolling_avg_time_s": r_s,
                     "rolling_avg": r_mi if metric == "distance" else r_s})
    tot_m = sum(r["distance_m"] for r in rows)
    tot_s = sum(r["time_s"] for r in rows)
    n = len(rows) or 1
    return {"metric": metric, "bucket": bucket, "start": first.isoformat(), "end": e.isoformat(),
            "sports": _sel_label(sel), "rows": rows,
            "total": {"distance_m": tot_m, "distance_mi": _miles(tot_m), "time_s": tot_s,
                      "runs": sum(r["runs"] for r in rows)},
            "avg_per_week": {"distance_mi": round(tot_m / METERS_PER_MILE / n, 2), "time_s": round(tot_s / n)}}


def _bucket_key(d: date, bucket: str, start: date) -> date:
    if bucket == "day":
        return d
    if bucket == "week":
        return _monday(d)
    if bucket == "month":
        return d.replace(day=1)
    return start


def _bucket_keys(start: date, end: date, bucket: str) -> list[date]:
    if bucket == "total":
        return [start]
    keys, d = [], _bucket_key(start, bucket, start)
    while d <= end:
        keys.append(d)
        if bucket == "day":
            d += timedelta(days=1)
        elif bucket == "week":
            d += timedelta(weeks=1)
        else:
            d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return keys


def _label(k: date, bucket: str) -> str:
    return {"total": "Total", "day": f"{k:%b} {k.day}", "week": f"Wk of {k:%b} {k.day}",
            "month": f"{k:%b %Y}"}[bucket]


def _zone_buckets(bucket, sports, start, end, db_path, today):
    _check(bucket, BUCKETS, "bucket")
    s, e = _range(start, end, today, default_days=DEFAULT_DAYS)
    sel = _parse_sports(sports, db_path)
    conn = connect(db_path)
    try:
        acts = _load(conn, sel, s, e, with_zones=True)
    finally:
        conn.close()
    data = {k: {"z": [0.0] * 6, "n": 0} for k in _bucket_keys(s, e, bucket)}
    for a in acts:
        if a["zone_seconds"] is None:
            continue
        b = data[_bucket_key(date.fromisoformat(a["day"]), bucket, s)]
        b["n"] += 1
        for i, v in enumerate(a["zone_seconds"]):
            b["z"][i] += v
    return s, e, sel, data


def zone_time(bucket: str = "total", sports=None, start=None, end=None, db_path=None,
              today: date | None = None) -> dict:
    s, e, sel, data = _zone_buckets(bucket, sports, start, end, db_path, today)
    rows = [{"bucket_start": k.isoformat(), "label": _label(k, bucket),
             "seconds": [round(x) for x in v["z"]], "total_s": round(sum(v["z"])), "activities": v["n"]}
            for k, v in data.items()]
    return {"bucket": bucket, "start": s.isoformat(), "end": e.isoformat(), "sports": _sel_label(sel),
            "rows": rows}


def _percents(z: list[float]) -> list[float] | None:
    total = sum(z)
    if total <= 0:
        return None
    p = [round(x / total * 100, 1) for x in z]
    i = p.index(max(p))
    p[i] = round(p[i] + (100.0 - sum(p)), 1)  # absorb rounding drift
    return p


def zone_share(bucket: str = "total", sports=None, start=None, end=None, db_path=None,
               today: date | None = None) -> dict:
    s, e, sel, data = _zone_buckets(bucket, sports, start, end, db_path, today)
    rows = [{"bucket_start": k.isoformat(), "label": _label(k, bucket), "percent": _percents(v["z"]),
             "total_s": round(sum(v["z"]))} for k, v in data.items()]
    return {"bucket": bucket, "start": s.isoformat(), "end": e.isoformat(), "sports": _sel_label(sel),
            "rows": rows}


def _easy(z: list[float]) -> tuple[float | None, float | None]:
    total = sum(z)
    if total <= 0:
        return None, None
    return round(sum(z[:3]) / total * 100, 1), round(sum(z[1:3]) / total * 100, 1)


def easy_share(bucket: str = "week", sports=None, start=None, end=None, db_path=None,
               today: date | None = None) -> dict:
    s, e, sel, data = _zone_buckets(bucket, sports, start, end, db_path, today)
    rows, allz = [], [0.0] * 6
    for k, v in data.items():
        easy, z12 = _easy(v["z"])
        rows.append({"bucket_start": k.isoformat(), "label": _label(k, bucket), "easy_pct": easy,
                     "z1_z2_pct": z12, "total_s": round(sum(v["z"]))})
        allz = [a + b for a, b in zip(allz, v["z"])]
    easy, z12 = _easy(allz)
    return {"bucket": bucket, "start": s.isoformat(), "end": e.isoformat(), "sports": _sel_label(sel),
            "rows": rows, "total": {"easy_pct": easy, "z1_z2_pct": z12, "total_s": round(sum(allz))}}


def _brief(a: dict) -> dict:
    return {"coros_id": a["coros_id"], "name": a["name"], "day": a["day"],
            "distance_mi": _miles(a["distance_m"]), "distance_m": round(a["distance_m"] or 0),
            "moving_s": a["moving_s"] or 0}


def longest_run_per_week(sports=None, start=None, end=None, db_path=None, today: date | None = None) -> dict:
    s, e = _range(start, end, today, default_weeks=DEFAULT_WEEKS)
    sel = _parse_sports(sports, db_path)
    first, last = _monday(s), _monday(e)
    conn = connect(db_path)
    try:
        acts = _load(conn, sel, first, e)
    finally:
        conn.close()
    rows = []
    for wk, items in _week_rows(acts, first, last).items():
        by_d = max(items, key=lambda a: a["distance_m"] or 0, default=None)
        by_t = max(items, key=lambda a: a["moving_s"] or 0, default=None)
        rows.append({"bucket_start": wk.isoformat(), "longest_distance": _brief(by_d) if by_d else None,
                     "longest_time": _brief(by_t) if by_t else None})
    return {"start": first.isoformat(), "end": e.isoformat(), "sports": _sel_label(sel), "rows": rows}


def summary(sports=None, start=None, end=None, db_path=None, today: date | None = None) -> dict:
    s, e = _range(start, end, today, default_days=DEFAULT_DAYS)
    sel = _parse_sports(sports, db_path)
    conn = connect(db_path)
    try:
        acts = _load(conn, sel, s, e, with_zones=True)
    finally:
        conn.close()
    dist = sum(a["distance_m"] or 0 for a in acts)
    secs = sum(a["moving_s"] or 0 for a in acts)
    days = (e - s).days + 1
    weeks = days / 7
    z = [0.0] * 6
    for a in acts:
        if a["zone_seconds"]:
            z = [x + y for x, y in zip(z, a["zone_seconds"])]
    ztot = sum(z)
    return {"start": s.isoformat(), "end": e.isoformat(), "sports": _sel_label(sel), "days": days,
            "activities": len(acts), "total_distance_m": round(dist), "total_distance_mi": _miles(dist),
            "total_time_s": int(secs), "avg_weekly_distance_mi": round(dist / METERS_PER_MILE / weeks, 2),
            "avg_weekly_time_s": round(secs / weeks), "zone_seconds": [round(x) for x in z],
            "zone_total_s": round(ztot),
            "z3_plus_pct": round(sum(z[3:]) / ztot * 100, 1) if ztot > 0 else None,
            "easy_pct": _easy(z)[0]}


def activities(sports=None, start=None, end=None, limit: int = 50, db_path=None,
               today: date | None = None) -> list[dict]:
    s, e = _parse_date(start, "start"), _parse_date(end, "end")
    if s and e and s > e:
        raise TrainingError(f"start {s} is after end {e}")
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise TrainingError("limit must be a whole number") from None
    if limit < 1:
        raise TrainingError("limit must be 1 or more")
    sel = _parse_sports(sports, db_path)
    conn = connect(db_path)
    try:
        acts = _load(conn, sel, s, e, with_zones=True)
    finally:
        conn.close()
    acts.sort(key=lambda a: (a["day"], a["start_local"] or ""), reverse=True)
    out = []
    for a in acts[:limit]:
        mi = (a["distance_m"] or 0) / METERS_PER_MILE
        out.append({"coros_id": a["coros_id"], "name": a["name"], "day": a["day"],
                    "start_local": a["start_local"], "sport_type": a["sport_type"],
                    "sport_name": a["sport_name"], "group": _group_of(a["sport_type"], a["name"]),
                    "is_indoor": bool(a["is_indoor"]), "distance_m": round(a["distance_m"] or 0),
                    "distance_mi": round(mi, 2), "moving_s": a["moving_s"],
                    "pace_s_per_mi": round(a["moving_s"] / mi) if mi > 0 and a["moving_s"] else None,
                    "avg_hr": a["avg_hr"], "max_hr": a["max_hr"], "training_load": a["training_load"],
                    "zone_seconds": [round(x) for x in a["zone_seconds"]] if a["zone_seconds"] else None})
    return out


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


def _to_int(v):
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _row_to_activity(row: dict) -> dict | None:
    """Map one COROS list row to activities columns (None when it has no usable id/day)."""
    coros_id = str(row.get("labelId") or "").strip()
    if not coros_id:
        return None
    sport = _to_int(row.get("sportType"))
    if sport is None:
        return None
    start_local = None
    if row.get("startTime"):
        offset = (_to_int(row.get("startTimezone")) or 0) * 15
        utc = datetime.fromtimestamp(int(row["startTime"]), tz=timezone.utc).replace(tzinfo=None)
        start_local = (utc + timedelta(minutes=offset)).strftime("%Y-%m-%dT%H:%M:%S")
    raw_day = str(row.get("date") or "")
    if len(raw_day) == 8 and raw_day.isdigit():
        day = f"{raw_day[:4]}-{raw_day[4:6]}-{raw_day[6:]}"
    elif start_local:
        day = start_local[:10]
    else:
        return None
    return {"coros_id": coros_id, "sport_type": sport, "sport_name": SPORT_NAMES.get(sport, f"Sport {sport}"),
            "name": row.get("name") or row.get("customName"), "is_indoor": int(sport in INDOOR_TYPES),
            "start_local": start_local, "day": day, "distance_m": _to_float(row.get("distance")),
            "moving_s": _to_int(row.get("workoutTime")), "elapsed_s": _to_int(row.get("totalTime")),
            "avg_hr": _to_int(row.get("avgHr")), "training_load": _to_int(row.get("trainingLoad")),
            "avg_cadence": _to_float(row.get("avgCadence")), "ascent_m": _to_float(row.get("ascent"))}


def _apply_detail(conn, activity_id: str, detail: dict) -> int:
    """Store HR zone rows + max HR from a detail `data` dict; returns number of zone rows written."""
    hr = next((z for z in (detail.get("zoneList") or []) if z.get("type") == 126), None)
    items = (hr or {}).get("zoneItemList") or []
    conn.execute("DELETE FROM activity_zone_time WHERE activity_id = ?", (activity_id,))
    n, bounds = 0, []
    for it in items:
        z = _to_int(it.get("zoneIndex"))
        if z is None or not 0 <= z <= 5:
            continue
        conn.execute("INSERT OR REPLACE INTO activity_zone_time(activity_id, zone, seconds, percent) "
                     "VALUES (?,?,?,?)", (activity_id, z, _to_int(it.get("second")) or 0,
                                          _to_float(it.get("percent"))))
        bounds.append({"zone": z, "low": _to_int(it.get("leftScope")), "high": _to_int(it.get("rightScope"))})
        n += 1
    max_hr = _to_int((detail.get("summary") or {}).get("maxHr"))
    raw = conn.execute("SELECT raw_json FROM activities WHERE id = ?", (activity_id,)).fetchone()["raw_json"]
    obj = json.loads(raw) if raw else {}
    obj["_detail"] = {"zones": n}
    conn.execute("UPDATE activities SET max_hr = COALESCE(?, max_hr), raw_json = ? WHERE id = ?",
                 (max_hr, json.dumps(obj), activity_id))
    if bounds:
        _put_config(conn, "hr_zone_bounds", json.dumps(sorted(bounds, key=lambda b: b["zone"])))
    return n


def sync(db_path=None, *, all_sports: bool = False, full: bool = False, today: date | None = None,
         fetch_list_pages=None, fetch_detail=None, fetch_account=None) -> dict:
    """Idempotent COROS -> SQLite sync. See the module docstring (SYNC) for the rules."""
    t = _today(today)
    conn = connect(db_path)
    live = None
    try:
        if not (fetch_list_pages and fetch_detail and fetch_account):
            live = _LiveCoros()
            fetch_list_pages = fetch_list_pages or live.fetch_list_pages
            fetch_detail = fetch_detail or live.fetch_detail
            fetch_account = fetch_account or live.fetch_account
        latest = conn.execute("SELECT MAX(day) d FROM activities").fetchone()["d"]
        if full or not latest:
            window_start = date(2015, 1, 1)
        else:
            window_start = date.fromisoformat(latest) - timedelta(days=INCREMENTAL_OVERLAP_DAYS)
        errors: list[str] = []
        try:
            rows = fetch_list_pages(window_start, t)
        except Exception as exc:
            raise TrainingError(f"COROS activity list failed: {exc}", 3) from exc
        inserted = updated = 0
        for row in rows:
            a = _row_to_activity(row)
            if not a:
                continue
            existing = conn.execute("SELECT id, raw_json FROM activities WHERE coros_id = ?",
                                    (a["coros_id"],)).fetchone()
            raw = dict(row)
            if existing:
                old = json.loads(existing["raw_json"] or "{}")
                if "_detail" in old:
                    raw["_detail"] = old["_detail"]
                conn.execute(
                    "UPDATE activities SET sport_type=?, sport_name=?, name=?, is_indoor=?, start_local=?, "
                    "day=?, distance_m=?, moving_s=?, elapsed_s=?, avg_hr=?, training_load=?, avg_cadence=?, "
                    "ascent_m=?, raw_json=?, synced_at=? WHERE id=?",
                    (a["sport_type"], a["sport_name"], a["name"], a["is_indoor"], a["start_local"], a["day"],
                     a["distance_m"], a["moving_s"], a["elapsed_s"], a["avg_hr"], a["training_load"],
                     a["avg_cadence"], a["ascent_m"], json.dumps(raw), _now_iso(), existing["id"]))
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO activities(id, coros_id, sport_type, sport_name, name, is_indoor, start_local, "
                    "day, distance_m, moving_s, elapsed_s, avg_hr, training_load, avg_cadence, ascent_m, "
                    "raw_json, synced_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), a["coros_id"], a["sport_type"], a["sport_name"], a["name"],
                     a["is_indoor"], a["start_local"], a["day"], a["distance_m"], a["moving_s"], a["elapsed_s"],
                     a["avg_hr"], a["training_load"], a["avg_cadence"], a["ascent_m"], json.dumps(raw),
                     _now_iso()))
                inserted += 1
        conn.commit()

        try:
            acct = fetch_account() or {}
            zd = acct.get("zoneData") or {}
            lthr, mx = _to_int(zd.get("lthr")), _to_int(acct.get("maxHr"))
            cur = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM training_config")}
            if lthr and cur.get("lthr") != str(lthr):
                _put_config(conn, "lthr", str(lthr))
            if mx and cur.get("max_hr") != str(mx):
                _put_config(conn, "max_hr", str(mx))
            if zd.get("lthrZone") is not None:
                _put_config(conn, "lthr_zone", json.dumps(zd["lthrZone"]))
            conn.commit()
        except Exception as exc:
            errors.append(f"account thresholds: {exc}")

        sel = _parse_sports("all" if all_sports else None, db_path)
        todo = [dict(r) for r in conn.execute(
            "SELECT a.id, a.coros_id, a.sport_type, a.name FROM activities a WHERE NOT EXISTS "
            "(SELECT 1 FROM activity_zone_time z WHERE z.activity_id = a.id) "
            "AND COALESCE(a.raw_json, '') NOT LIKE '%\"_detail\"%' ORDER BY a.day DESC")]
        todo = [a for a in todo if _matches(sel, a["sport_type"], a["name"])]
        fetched = failed = 0
        for a in todo:
            try:
                detail = fetch_detail(a["coros_id"], a["sport_type"]) or {}
                _apply_detail(conn, a["id"], detail)
                conn.commit()
                fetched += 1
            except Exception as exc:
                conn.rollback()
                failed += 1
                errors.append(f"{a['coros_id']} ({a['name']}): {exc}")
        return {"window_start": window_start.isoformat(), "listed": len(rows), "inserted": inserted,
                "updated": updated, "detail_attempted": len(todo), "detail_fetched": fetched,
                "detail_failed": failed, "errors": errors, "partial": bool(errors)}
    finally:
        if live:
            live.close()
        conn.close()


# ---------------------------------------------------------------------------
# COROS network layer: the only code that touches the network. Reuses the Playwright login session
# from scripts/fitness (state/coros_session.json). Injected into sync() so tests use fakes.
# ---------------------------------------------------------------------------


class _LiveCoros:
    """One headless browser session shared by the three fetch_* callables; opened lazily."""

    def __init__(self):
        self._pw = self._browser = self._page = None
        self._headers: dict = {}

    def _open(self):
        if self._page:
            return
        sys.path.insert(0, str(REPO_ROOT / "scripts" / "fitness"))
        from playwright.sync_api import sync_playwright
        from coros_session import SESSION_FILE
        from schedule_ops import _get_access_token, ensure_logged_in

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = self._browser.new_context(storage_state=str(SESSION_FILE) if SESSION_FILE.exists() else None)
        self._page = ctx.new_page()
        ensure_logged_in(self._page)
        self._headers = {"accessToken": _get_access_token(self._page)}

    @staticmethod
    def _base() -> str:
        sys.path.insert(0, str(REPO_ROOT / "scripts" / "fitness"))
        from coros_web import TEAMAPI_BASE
        return TEAMAPI_BASE

    @staticmethod
    def _data(resp, what: str) -> dict:
        body = resp.json()
        if body.get("result") != "0000":
            raise RuntimeError(f"{what} failed: {body.get('message') or body}")
        return body.get("data") or {}

    def fetch_list_pages(self, start: date, end: date) -> list[dict]:
        self._open()
        out, number = [], 1
        while number <= 200:  # safety stop only; full history is ~16 pages of 50
            resp = self._page.request.get(f"{self._base()}/activity/query", headers=self._headers, params={
                "size": "50", "pageNumber": str(number), "modeList": "",
                "startDay": start.strftime("%Y%m%d"), "endDay": end.strftime("%Y%m%d")})
            rows = self._data(resp, "activity/query").get("dataList") or []
            out.extend(rows)
            if len(rows) < 50:
                break
            number += 1
        return out

    def fetch_detail(self, coros_id: str, sport_type: int) -> dict:
        self._open()
        resp = self._page.request.post(f"{self._base()}/activity/detail/query", headers=self._headers,
                                       params={"labelId": str(coros_id), "sportType": str(sport_type),
                                               "screenW": "1200"})
        return self._data(resp, "activity/detail/query")

    def fetch_account(self) -> dict:
        self._open()
        return self._data(self._page.request.get(f"{self._base()}/account/query", headers=self._headers),
                          "account/query")

    def close(self):
        for obj, meth in ((self._browser, "close"), (self._pw, "stop")):
            try:
                if obj:
                    getattr(obj, meth)()
            except Exception:
                pass
