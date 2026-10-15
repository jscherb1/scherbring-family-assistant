"""Insights engine: pluggable daily-metric sources + correlation stats against the day rating.

Zero-dependency (Python stdlib only), same SQLite file as the rest of the assistant.

How it grows by itself
  - Every module in scripts/insight_sources/ (except names starting with "_") is a source. It exposes
        SOURCE  = {"name": ..., "description": ..., "kind": "auto" | "manual"}
        collect(conn, start, end, today) -> {metric: {YYYY-MM-DD: float}}
    `refresh()` runs every source and upserts the values into the insight_daily table. Dropping a new
    module into that folder is all it takes for its metrics to flow into the analysis.
  - Metrics are named "<group>:<name>" (e.g. habit:Workouts, weather:precip_in, coros:sleep_hours).

Analysis
  - For each metric (same day, and the day before as a lag-1 variant) it measures how it moves with the
    rating: Spearman rank correlation for varied values, a with/without comparison for 0/1 metrics,
    plus a permutation p-value. With many metrics tested, some will look significant by chance, so
    results carry a confidence label and the narrative must call them leads, not proof.
  - Too little data is reported as such rather than overclaimed (MIN_DAYS paired days per metric).
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import random
import sqlite3
import sys
import uuid
import zlib
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import dayrating  # noqa: E402

MIN_DAYS = 10          # paired days needed before a metric is analysed
SOLID_DAYS = 28        # paired days needed before a finding can be "solid"
MIN_DAYS_BY_PREFIX = {"coros:wellness_": 14}  # sparse, irregularly-taken readings need more paired days
MIN_GROUP = 3          # days in each side of a 0/1 comparison
PERMUTATIONS = 2000


class InsightError(Exception):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.message = message
        self.code = code


def connect(db_path=None) -> sqlite3.Connection:
    return dayrating.connect(db_path)


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------


def discover_sources() -> list:
    """Import every module in scripts/insight_sources/ that defines SOURCE and collect()."""
    import insight_sources

    mods = []
    for info in pkgutil.iter_modules(insight_sources.__path__):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"insight_sources.{info.name}")
        if hasattr(mod, "SOURCE") and hasattr(mod, "collect"):
            mods.append(mod)
    return sorted(mods, key=lambda m: m.SOURCE["name"])


def list_sources(*, db_path=None) -> list[dict]:
    """Each source with the metrics and day coverage currently stored for it."""
    conn = connect(db_path)
    try:
        out = []
        for mod in discover_sources():
            rows = conn.execute("SELECT COUNT(DISTINCT metric) m, COUNT(*) n, MIN(day) lo, MAX(day) hi "
                                "FROM insight_daily WHERE source = ?", (mod.SOURCE["name"],)).fetchone()
            out.append({**mod.SOURCE, "metrics": rows["m"], "values": rows["n"],
                        "first_day": rows["lo"], "last_day": rows["hi"]})
        return out
    finally:
        conn.close()


# Sources the 30-minute timer keeps current. Excluded: calendar_day (static), calendar_load (agent-fed) and
# coros_wellness (Training Hub web login; a frequent re-login would sign COROS out elsewhere).
LIVE_SOURCES = "coros_health,weather,habits"
LIVE_DAYS = 3  # a short window keeps COROS to a few calls instead of the 60-day wellness backfill


def refresh_live(*, db_path=None) -> dict:
    return refresh(days=LIVE_DAYS, only=LIVE_SOURCES, db_path=db_path)


def freshness(*, db_path=None) -> dict:
    """Latest write time per source, for the hub footer."""
    conn = connect(db_path)
    try:
        return {r["source"]: r["at"] for r in conn.execute(
            "SELECT source, MAX(updated_at) at FROM insight_daily GROUP BY source")}
    finally:
        conn.close()


def refresh(*, days: int = 90, only: str | None = None, db_path=None, today: date | None = None) -> dict:
    """Run every source (or the named ones, comma-separated) for the last `days` days and store the
    values. A failing source is reported and skipped; the others still run."""
    wanted = {n.strip() for n in only.split(",") if n.strip()} if only else None
    today = today or datetime.now().date()
    start = today - timedelta(days=days - 1)
    conn = connect(db_path)
    report = {}
    try:
        for mod in discover_sources():
            name = mod.SOURCE["name"]
            if wanted and name not in wanted:
                continue
            try:
                data = mod.collect(conn, start, today, today)
            except Exception as exc:  # a flaky network source must not block the rest
                report[name] = {"error": f"{type(exc).__name__}: {exc}"}
                continue
            count, now = 0, _now_iso()
            for metric, series in data.items():
                for day, value in series.items():
                    if value is None:
                        continue
                    conn.execute(
                        "INSERT INTO insight_daily (metric, day, value, source, updated_at) VALUES (?,?,?,?,?) "
                        "ON CONFLICT(metric, day) DO UPDATE SET value = excluded.value, "
                        "source = excluded.source, updated_at = excluded.updated_at",
                        (metric, day, float(value), name, now))
                    count += 1
            conn.commit()
            report[name] = {"metrics": len(data), "values": count}
        if wanted and wanted - set(report):
            raise InsightError(f"no source named {sorted(wanted - set(report))[0]!r}", 1)
        return report
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def _pearson(x: list[float], y: list[float]) -> float | None:
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    sx = sum((a - mx) ** 2 for a in x)
    sy = sum((b - my) ** 2 for b in y)
    if sx == 0 or sy == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / (sx * sy) ** 0.5


def spearman(x: list[float], y: list[float]) -> float | None:
    return _pearson(_ranks(x), _ranks(y))


def _perm_p(x: list[float], y: list[float], observed: float, seed: int) -> float:
    """Two-sided permutation p-value for |statistic| on shuffled y (deterministic)."""
    rng = random.Random(seed)
    ys, hits = list(y), 0
    for _ in range(PERMUTATIONS):
        rng.shuffle(ys)
        r = _pearson(x, ys)
        if r is not None and abs(r) >= abs(observed) - 1e-12:
            hits += 1
    return (hits + 1) / (PERMUTATIONS + 1)


def _confidence(n: int, p: float) -> str:
    if n >= SOLID_DAYS and p < 0.01:
        return "solid"
    if p < 0.05:
        return "emerging"
    if p < 0.15:
        return "hint"
    return "none"


def min_days_for(metric: str) -> int:
    return max([MIN_DAYS] + [n for prefix, n in MIN_DAYS_BY_PREFIX.items() if metric.startswith(prefix)])


def analyze_pair(x: list[float], y: list[float], seed: int = 0, min_days: int = MIN_DAYS) -> dict | None:
    """Relationship of metric x to rating y over paired days, or None when there isn't enough data."""
    n = len(x)
    if n < min_days or len(set(x)) < 2 or len(set(y)) < 2:
        return None
    binary = set(x) <= {0.0, 1.0}
    out = {"n": n}
    if binary:
        with_, without = [b for a, b in zip(x, y) if a == 1.0], [b for a, b in zip(x, y) if a == 0.0]
        if len(with_) < MIN_GROUP or len(without) < MIN_GROUP:
            return None
        out.update(kind="yes_no", days_with=len(with_), days_without=len(without),
                   avg_rating_with=round(sum(with_) / len(with_), 2),
                   avg_rating_without=round(sum(without) / len(without), 2))
        out["effect"] = round(out["avg_rating_with"] - out["avg_rating_without"], 2)
    else:
        out["kind"] = "numeric"
    r = spearman(x, y)
    if r is None:
        return None
    out["correlation"] = round(r, 3)
    out["p_value"] = round(_perm_p(_ranks(x), _ranks(y), r, seed), 4)
    out["confidence"] = _confidence(n, out["p_value"])
    out["direction"] = "higher rating" if r > 0 else "lower rating"
    return out


def _series(conn: sqlite3.Connection) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for r in conn.execute("SELECT metric, day, value FROM insight_daily"):
        out.setdefault(r["metric"], {})[r["day"]] = r["value"]
    return out


def analyze(*, days: int = 90, db_path=None, today: date | None = None) -> dict:
    """The statistics behind the report: rating summary, ranked findings, and what's missing."""
    today = today or datetime.now().date()
    start = today - timedelta(days=days - 1)
    ratings = {r["date"]: r for r in dayrating.list_ratings(days=days, db_path=db_path, today=today)}
    scores = {d: float(r["score"]) for d, r in ratings.items()}
    conn = connect(db_path)
    try:
        series = _series(conn)
    finally:
        conn.close()

    findings, skipped = [], []
    for metric in sorted(series):
        for lag in (0, 1):
            label = metric if lag == 0 else f"{metric} (previous day)"
            xs, ys = [], []
            for day, score in sorted(scores.items()):
                src_day = (date.fromisoformat(day) - timedelta(days=lag)).isoformat()
                if src_day in series[metric]:
                    xs.append(series[metric][src_day])
                    ys.append(score)
            res = analyze_pair(xs, ys, seed=zlib.crc32(f"{metric}|{lag}".encode()),
                               min_days=min_days_for(metric))
            if res is None:
                skipped.append({"metric": label, "paired_days": len(xs), "days_with_data": len(series[metric]),
                                "min_days_needed": min_days_for(metric)})
            else:
                findings.append({"metric": label, "lag_days": lag, **res})
    findings.sort(key=lambda f: (f["p_value"], -abs(f["correlation"])))

    by_weekday: dict[str, list[float]] = {}
    for day, score in scores.items():
        by_weekday.setdefault(date.fromisoformat(day).strftime("%A"), []).append(score)
    values = list(scores.values())
    ordered = sorted(ratings.values(), key=lambda r: r["date"])
    return {
        "window": {"start": start.isoformat(), "end": today.isoformat(), "days": days},
        "ratings": {
            "count": len(values),
            "average": round(sum(values) / len(values), 2) if values else None,
            "best": max(ordered, key=lambda r: r["score"]) if ordered else None,
            "worst": min(ordered, key=lambda r: r["score"]) if ordered else None,
            "by_weekday": {d: round(sum(v) / len(v), 2) for d, v in by_weekday.items()},
            "recent": ordered[-14:],
        },
        "findings": findings,
        "not_enough_data": skipped,
        "enough_data": len(values) >= MIN_DAYS,
        "min_days_needed": MIN_DAYS,
        "sources": list_sources(db_path=db_path),
        "caveats": ["Many metrics are tested at once, so some will look meaningful by chance: treat findings as "
                    "leads, not proof.",
                    "Correlation is not causation, and a day's rating also reflects things no source captures."],
    }


# ---------------------------------------------------------------------------
# stored AI narratives (written by the `insights` subagent, shown on the dashboard)
# ---------------------------------------------------------------------------


def save_report(narrative: str, *, days: int = 90, db_path=None, today: date | None = None) -> dict:
    narrative = (narrative or "").strip()
    if not narrative:
        raise InsightError("narrative is required")
    stats = analyze(days=days, db_path=db_path, today=today)
    conn = connect(db_path)
    try:
        rid = str(uuid.uuid4())
        conn.execute("INSERT INTO insight_reports (id, created_at, period_days, stats_json, narrative) "
                     "VALUES (?,?,?,?,?)", (rid, _now_iso(), days, json.dumps(stats), narrative))
        conn.commit()
        return {"id": rid, "saved": True}
    finally:
        conn.close()


def latest_report(*, db_path=None) -> dict | None:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT id, created_at, period_days, narrative FROM insight_reports "
                         "ORDER BY created_at DESC LIMIT 1").fetchone()
        return dict(r) if r else None
    finally:
        conn.close()
