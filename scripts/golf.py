"""Golf rounds + handicap: the single shared service.

Zero-dependency (Python stdlib only), same SQLite file as state_store.py
(state/agent_results.db, schema in state/schema.sql).

Both front ends are thin wrappers over this module, so there is exactly one
implementation of the business rules (differentials, handicap, logging, undo):
  - scripts/golf_store.py   the CLI the `golf` subagent runs
  - dashboard/routers/golf.py   the web UI and its /api/golf* twins

Every function takes an optional `db_path` (default: state/agent_results.db). Failures raise
GolfError with a CLI-style code: 1 = not found, 2 = bad input.

Handicap rules (mirror Justin's "Handicap - 18 holes only" sheet tab)
  - Differential = (score - course rating) * 113 / slope, per 18-hole round.
  - Take the most recent `window` (20) rounds by date, keep the lowest `best_n` (8)
    differentials, average them and multiply by `factor` (0.96). One decimal.
  - With fewer than `best_n` rounds on file the average runs over what exists and the result
    is flagged provisional.
The local DB is the source of truth. New rounds are also appended to the Google Sheet by
scripts/golf_sheet.py (best-effort mirror; a failed mirror never blocks the log).
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "state" / "agent_results.db"
SCHEMA_PATH = REPO_ROOT / "state" / "schema.sql"

SLOPE_BASE = 113
CONFIG_DEFAULTS = {
    "window": "20",
    "best_n": "8",
    "factor": "0.96",
    "sheet_id": "1xxKbfM4s8iZiK27eo_nSXM94yBQsKX-4371AbMm8n1w",
    "sheet_tab": "Handicap - 18 holes only",
    "sheet_mirror": "1",
}


class GolfError(Exception):
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


def parse_date(value) -> str:
    """Accept YYYY-MM-DD plus the sheet's m/d/yy, mm/dd/yy and m/d/yyyy; return YYYY-MM-DD."""
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise GolfError(f"invalid date {value!r}, expected YYYY-MM-DD")


def _num(value, field: str, lo: float, hi: float) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise GolfError(f"{field} must be a number") from None
    if not lo <= num <= hi:
        raise GolfError(f"{field} {num:g} is outside {lo:g}-{hi:g}")
    return num


def _clean(value: str | None, field: str, required: bool = False) -> str | None:
    text = (value or "").strip()
    if required and not text:
        raise GolfError(f"{field} is required")
    return text or None


def differential(score: float, rating: float, slope: float) -> float:
    return (score - rating) * SLOPE_BASE / slope


def get_config(db_path=None) -> dict:
    with connect(db_path) as conn:
        stored = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM golf_config")}
    return {**CONFIG_DEFAULTS, **stored}


def set_config(key: str, value: str, db_path=None) -> dict:
    if key not in CONFIG_DEFAULTS:
        raise GolfError(f"unknown config key {key!r} (known: {', '.join(CONFIG_DEFAULTS)})")
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO golf_config (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, str(value), _now_iso()),
        )
    return get_config(db_path)


# ---------------------------------------------------------------------------
# handicap
# ---------------------------------------------------------------------------


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["differential"] = round(d["differential"], 2)
    d["sheet_synced"] = bool(d["sheet_synced"])
    return d


def _handicap_from(rounds: list[dict], cfg: dict) -> dict:
    """`rounds` newest first, each with id + differential + score."""
    window, best_n, factor = int(cfg["window"]), int(cfg["best_n"]), float(cfg["factor"])
    recent = rounds[:window]
    if not recent:
        return {"handicap": None, "rounds_considered": 0, "counted_ids": [], "provisional": True,
                "window": window, "best_n": best_n, "factor": factor}
    best = sorted(recent, key=lambda r: r["differential"])[:best_n]
    mean = sum(r["differential"] for r in best) / len(best)
    return {
        "handicap": round(mean * factor, 1),
        "rounds_considered": len(recent),
        "counted_ids": [r["id"] for r in best],
        "provisional": len(recent) < best_n,
        "window": window, "best_n": best_n, "factor": factor,
    }


def _all_rounds(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM golf_rounds ORDER BY played_on DESC, created_at DESC").fetchall()
    return [_row(r) | {"differential_raw": r["differential"]} for r in rows]


def _calc_rounds(rounds: list[dict]) -> list[dict]:
    return [{"id": r["id"], "differential": r["differential_raw"]} for r in rounds]


def get_handicap(db_path=None) -> dict:
    cfg = get_config(db_path)
    with connect(db_path) as conn:
        rounds = _all_rounds(conn)
    result = _handicap_from(_calc_rounds(rounds), cfg)
    recent = rounds[:10]
    result.update({
        "rounds_total": len(rounds),
        "last_round": rounds[0] if rounds else None,
        "best_score": min((r["score"] for r in rounds), default=None),
        "avg_score_last_10": round(sum(r["score"] for r in recent) / len(recent), 1) if recent else None,
    })
    return result


def list_rounds(limit: int = 20, db_path=None) -> list[dict]:
    """Newest first; each round says whether it currently counts toward the handicap."""
    cfg = get_config(db_path)
    with connect(db_path) as conn:
        rounds = _all_rounds(conn)
    counted = set(_handicap_from(_calc_rounds(rounds), cfg)["counted_ids"])
    window = int(cfg["window"])
    out = []
    for i, r in enumerate(rounds[: max(1, limit)]):
        r = {k: v for k, v in r.items() if k != "differential_raw"}
        r["counts"] = r["id"] in counted
        r["in_window"] = i < window
        out.append(r)
    return out


def get_round(round_id: str, db_path=None) -> dict:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM golf_rounds WHERE id = ?", (round_id,)).fetchone()
    if not row:
        raise GolfError(f"no round with id {round_id}", 1)
    return _row(row)


# ---------------------------------------------------------------------------
# courses
# ---------------------------------------------------------------------------


def list_courses(db_path=None) -> list[dict]:
    with connect(db_path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM golf_courses ORDER BY lower(name)")]


def resolve_course(ref: str, db_path=None) -> dict:
    """Course by exact name (case-insensitive) or a unique name fragment."""
    ref = (ref or "").strip().lower()
    if not ref:
        raise GolfError("course is required")
    courses = list_courses(db_path)
    exact = [c for c in courses if c["name"].lower() == ref]
    if exact:
        return exact[0]
    partial = [c for c in courses if ref in c["name"].lower()]
    if len(partial) == 1:
        return partial[0]
    if partial:
        raise GolfError(f"course {ref!r} is ambiguous: " + "; ".join(c["name"] for c in partial))
    raise GolfError(f"no saved course matching {ref!r}; give --rating and --slope", 1)


def save_course(name: str, rating, slope, tee: str | None = None, db_path=None) -> dict:
    name = _clean(name, "course", required=True)
    rating_f = _num(rating, "rating", 55, 80)
    slope_f = int(_num(slope, "slope", 55, 155))
    now = _now_iso()
    with connect(db_path) as conn:
        row = conn.execute("SELECT id FROM golf_courses WHERE lower(name) = lower(?)", (name,)).fetchone()
        if row:
            conn.execute("UPDATE golf_courses SET rating=?, slope=?, tee=COALESCE(?, tee), updated_at=? WHERE id=?",
                         (rating_f, slope_f, _clean(tee, "tee"), now, row["id"]))
            cid = row["id"]
        else:
            cid = str(uuid.uuid4())
            conn.execute("INSERT INTO golf_courses (id, name, tee, rating, slope, created_at, updated_at) "
                         "VALUES (?,?,?,?,?,?,?)", (cid, name, _clean(tee, "tee"), rating_f, slope_f, now, now))
        return dict(conn.execute("SELECT * FROM golf_courses WHERE id = ?", (cid,)).fetchone())


# ---------------------------------------------------------------------------
# rounds
# ---------------------------------------------------------------------------


def _prepare(played_on, course, score, rating, slope, db_path) -> dict:
    day = parse_date(played_on)
    if day > date.today().isoformat():
        raise GolfError(f"date {day} is in the future")
    course = _clean(course, "course", required=True)
    if rating in (None, "") or slope in (None, ""):
        saved = resolve_course(course, db_path)
        course = saved["name"]
        rating = saved["rating"] if rating in (None, "") else rating
        slope = saved["slope"] if slope in (None, "") else slope
    rating_f = _num(rating, "rating", 55, 80)
    slope_f = int(_num(slope, "slope", 55, 155))
    score_i = int(_num(score, "score", 40, 200))
    if score_i < rating_f - 25:
        raise GolfError(f"score {score_i} looks too low for an 18-hole round (is this a 9-hole round?)")
    return {"played_on": day, "course": course, "rating": rating_f, "slope": slope_f, "score": score_i,
            "differential": differential(score_i, rating_f, slope_f)}


def preview_round(played_on, course, score, rating=None, slope=None, db_path=None) -> dict:
    """What logging this round would do. Writes nothing."""
    p = _prepare(played_on, course, score, rating, slope, db_path)
    cfg = get_config(db_path)
    with connect(db_path) as conn:
        rounds = _all_rounds(conn)
    before = _handicap_from(_calc_rounds(rounds), cfg)
    dated = [(r["played_on"], r["id"], r["differential_raw"]) for r in rounds] + [(p["played_on"], "__new__", p["differential"])]
    dated.sort(key=lambda t: t[0], reverse=True)
    merged = [{"id": i, "differential": d} for _, i, d in dated]
    after = _handicap_from(merged, cfg)
    return {
        "round": {**p, "differential": round(p["differential"], 2)},
        "handicap_before": before["handicap"],
        "handicap_after": after["handicap"],
        "change": None if before["handicap"] is None else round(after["handicap"] - before["handicap"], 1),
        "counts": "__new__" in after["counted_ids"],
    }


def add_round(played_on, course, score, rating=None, slope=None, tee=None, notes=None,
              mirror: bool = True, db_path=None) -> dict:
    """Log an 18-hole round. Rating/slope default from the saved course; new ones are saved for next time.

    Returns the round, handicap before/after, and the sheet-mirror outcome under `sheet`.
    """
    p = _prepare(played_on, course, score, rating, slope, db_path)
    before = get_handicap(db_path)["handicap"]
    save_course(p["course"], p["rating"], p["slope"], tee=tee, db_path=db_path)
    rid, now = str(uuid.uuid4()), _now_iso()
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO golf_rounds (id, played_on, course, tee, rating, slope, score, differential, notes, "
            "sheet_synced, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,0,?,?)",
            (rid, p["played_on"], p["course"], _clean(tee, "tee"), p["rating"], p["slope"], p["score"],
             p["differential"], _clean(notes, "notes"), now, now))
    result = {"round": get_round(rid, db_path), "handicap_before": before,
              "handicap_after": get_handicap(db_path)["handicap"],
              "sheet": {"synced": False, "skipped": True}}
    if mirror and get_config(db_path)["sheet_mirror"] == "1":
        result["sheet"] = sync_round(rid, db_path=db_path)
        result["round"] = get_round(rid, db_path)
    return result


def sync_round(round_id: str, db_path=None) -> dict:
    """Append one round to the Google Sheet. Never raises; reports {synced, error}."""
    try:
        import golf_sheet
        r = get_round(round_id, db_path)
        golf_sheet.append_round(r, get_config(db_path))
    except Exception as exc:  # noqa: BLE001 - mirror is best-effort by design
        return {"synced": False, "error": str(exc)}
    with connect(db_path) as conn:
        conn.execute("UPDATE golf_rounds SET sheet_synced = 1 WHERE id = ?", (round_id,))
    return {"synced": True}


def sync_pending(db_path=None) -> dict:
    with connect(db_path) as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM golf_rounds WHERE sheet_synced = 0 ORDER BY played_on, created_at")]
    results = [sync_round(i, db_path) | {"id": i} for i in ids]
    return {"pending": len(ids), "synced": sum(1 for r in results if r["synced"]), "results": results}


def update_round(round_id: str, db_path=None, **changes) -> dict:
    cur = get_round(round_id, db_path)
    merged = {k: changes.get(k) if changes.get(k) not in (None, "") else cur[k]
              for k in ("played_on", "course", "score", "rating", "slope")}
    p = _prepare(merged["played_on"], merged["course"], merged["score"], merged["rating"], merged["slope"], db_path)
    tee = changes.get("tee") if changes.get("tee") is not None else cur["tee"]
    notes = changes.get("notes") if changes.get("notes") is not None else cur["notes"]
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE golf_rounds SET played_on=?, course=?, tee=?, rating=?, slope=?, score=?, differential=?, "
            "notes=?, updated_at=? WHERE id=?",
            (p["played_on"], p["course"], tee, p["rating"], p["slope"], p["score"], p["differential"], notes,
             _now_iso(), round_id))
    return get_round(round_id, db_path)


def delete_round(round_id: str, db_path=None) -> dict:
    r = get_round(round_id, db_path)
    with connect(db_path) as conn:
        conn.execute("DELETE FROM golf_rounds WHERE id = ?", (round_id,))
    return {"deleted": r, "handicap": get_handicap(db_path)["handicap"],
            "sheet_note": "The Google Sheet row (if mirrored) is not removed; delete it there by hand."
            if r["sheet_synced"] else None}


def undo_last(db_path=None) -> dict:
    """Remove the most recently entered round (by entry time, not play date)."""
    with connect(db_path) as conn:
        row = conn.execute("SELECT id FROM golf_rounds ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
    if not row:
        raise GolfError("no rounds to undo", 1)
    return delete_round(row["id"], db_path)


def import_rows(rows, db_path=None) -> dict:
    """One-time load of sheet rows [date, course, rating, slope, score]. Skips duplicates and bad rows.

    Imported rounds are marked already synced, and their courses are saved for later autofill
    (the latest-dated rating/slope per course wins).
    """
    existing = set()
    with connect(db_path) as conn:
        for r in conn.execute("SELECT played_on, lower(course) c, score, rating, slope FROM golf_rounds"):
            existing.add((r["played_on"], r["c"], r["score"], r["rating"], r["slope"]))
    parsed, skipped = [], []
    for n, row in enumerate(rows, start=1):
        try:
            day = parse_date(row[0])
            course = _clean(row[1], "course", required=True)
            rating, slope = _num(row[2], "rating", 55, 80), int(_num(row[3], "slope", 55, 155))
            score = int(_num(row[4], "score", 40, 200))
        except (GolfError, IndexError) as exc:
            skipped.append({"row": n, "reason": getattr(exc, "message", "short row")})
            continue
        key = (day, course.lower(), score, rating, slope)
        if key in existing:
            skipped.append({"row": n, "reason": "duplicate"})
            continue
        existing.add(key)
        parsed.append((day, course, rating, slope, score))
    now = _now_iso()
    with connect(db_path) as conn:
        for day, course, rating, slope, score in parsed:
            conn.execute(
                "INSERT INTO golf_rounds (id, played_on, course, tee, rating, slope, score, differential, notes, "
                "sheet_synced, created_at, updated_at) VALUES (?,?,?,NULL,?,?,?,?,NULL,1,?,?)",
                (str(uuid.uuid4()), day, course, rating, slope, score, differential(score, rating, slope), now, now))
    for day, course, rating, slope, _score in sorted(parsed):  # oldest first so the latest wins
        save_course(course, rating, slope, db_path=db_path)
    return {"imported": len(parsed), "skipped": skipped, "handicap": get_handicap(db_path)["handicap"]}
