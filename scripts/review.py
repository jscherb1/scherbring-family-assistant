"""Weekly step-back review: the single shared service.

Zero-dependency (Python stdlib only), same SQLite file as state_store.py
(state/agent_results.db, schema in state/schema.sql).

Both front ends are thin wrappers over this module:
  - scripts/review_store.py        the CLI the `review` subagent runs
  - dashboard/routers/review.py    the /review page and its /api/review twin

What it does
  - gather(): a compact cross-domain digest (calendar, tasks, money, wellness, habits, chores, day ratings and
    journal, home, lawn, profile dates, scheduled-task results, system health, recent agent results) plus the
    findings from earlier runs and how the user responded. Each section is read through the existing service
    for that area and is fault tolerant: a failure becomes {"error": ...} for that section only. The whole
    digest is bounded (MAX_CHARS) and carries counts and summaries, never raw dumps. It never includes kids'
    memory text, and the profile contributes names and dates only.
  - save_run(summary, findings): stores one run and up to MAX_FINDINGS findings. A finding has a stable `key`;
    raising the same key again updates that finding rather than adding a duplicate.
  - list_findings, set_status, latest_run: what the dashboard and the agent read and the user acts on.

Every function takes an optional `db_path` (default: state/agent_results.db) and, where dates matter, an
optional `today` so tests can pin the clock. Failures raise ReviewError with a CLI-style code:
1 = not found, 2 = bad input.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "state" / "agent_results.db"
SCHEMA_PATH = REPO_ROOT / "state" / "schema.sql"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

STATUSES = ("new", "accepted", "dismissed", "done")
DOMAINS = ("calendar", "tasks", "money", "wellness", "habits", "chores", "home", "lawn", "family", "system",
           "assistant", "cross-domain", "other")
MAX_FINDINGS = 5
MAX_CHARS = 30000          # bound on the gather() digest, serialised
PAST_FINDINGS_DAYS = 120
FIELD_LIMITS = {"title": 140, "why_it_matters": 700, "evidence": 900, "suggested_action": 500}
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9:_.\-]{2,79}$")
# Agents whose stored results can hold kids' memories or profile values; never put them in the digest.
SENSITIVE_AGENTS = ("kids-memory", "kid_memories", "kids_memory", "profile")


class ReviewError(Exception):
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


def _clip(value, n: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------------------------------------------------------------------------
# findings
# ---------------------------------------------------------------------------


def _clean_finding(raw, index: int) -> dict:
    if not isinstance(raw, dict):
        raise ReviewError(f"finding {index + 1} must be an object")
    out = {}
    key = str(raw.get("key") or "").strip().lower()
    if not KEY_RE.match(key):
        raise ReviewError(f"finding {index + 1}: key must be a stable slug (3-80 chars of a-z 0-9 : _ . -), got {key!r}")
    out["key"] = key
    domain = str(raw.get("domain") or "").strip().lower()
    if domain not in DOMAINS:
        raise ReviewError(f"finding {index + 1}: domain must be one of: {', '.join(DOMAINS)}")
    out["domain"] = domain
    for field, limit in FIELD_LIMITS.items():
        value = " ".join(str(raw.get(field) or "").split())
        if not value:
            raise ReviewError(f"finding {index + 1}: {field} is required")
        if len(value) > limit:
            raise ReviewError(f"finding {index + 1}: {field} is too long ({len(value)} > {limit} characters)")
        out[field] = value
    return out


def save_run(summary: str, findings, *, db_path=None) -> dict:
    """Store one review run. `findings` may be empty ("nothing new this week" is a valid run)."""
    summary = " ".join(str(summary or "").split())
    if not summary:
        raise ReviewError("summary is required")
    if len(summary) > 1200:
        raise ReviewError("summary is too long (max 1200 characters)")
    if findings is None:
        findings = []
    if not isinstance(findings, list):
        raise ReviewError("findings must be a list")
    if len(findings) > MAX_FINDINGS:
        raise ReviewError(f"at most {MAX_FINDINGS} findings per run (got {len(findings)}); keep only the most valuable")
    cleaned = [_clean_finding(f, i) for i, f in enumerate(findings)]
    keys = [f["key"] for f in cleaned]
    if len(set(keys)) != len(keys):
        raise ReviewError("finding keys must be unique within a run")

    now = _now_iso()
    run_id = str(uuid.uuid4())
    conn = connect(db_path)
    try:
        conn.execute("INSERT INTO review_runs (id, created_at, summary, finding_count) VALUES (?,?,?,?)",
                     (run_id, now, summary, len(cleaned)))
        results = []
        for f in cleaned:
            row = conn.execute("SELECT id, status, times_raised FROM review_findings WHERE key = ?",
                               (f["key"],)).fetchone()
            if row is None:
                fid = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO review_findings (id, run_id, key, domain, title, why_it_matters, evidence, "
                    "suggested_action, status, times_raised, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?, 'new', 1, ?, ?)",
                    (fid, run_id, f["key"], f["domain"], f["title"], f["why_it_matters"], f["evidence"],
                     f["suggested_action"], now, now))
                results.append({"id": fid, "key": f["key"], "result": "created", "status": "new"})
            else:
                # Raised again. A dismissed or finished item coming back means the agent judged that something
                # materially changed, so it returns to "new"; an accepted one stays accepted.
                reopened = row["status"] in ("dismissed", "done")
                status = "new" if reopened else row["status"]
                conn.execute(
                    "UPDATE review_findings SET run_id=?, domain=?, title=?, why_it_matters=?, evidence=?, "
                    "suggested_action=?, status=?, times_raised=?, updated_at=? WHERE id=?",
                    (run_id, f["domain"], f["title"], f["why_it_matters"], f["evidence"], f["suggested_action"],
                     status, row["times_raised"] + 1, now, row["id"]))
                results.append({"id": row["id"], "key": f["key"], "result": "reopened" if reopened else "updated",
                                "status": status})
        conn.commit()
        return {"run_id": run_id, "saved": True, "created_at": now, "findings": results}
    finally:
        conn.close()


def _finding(row: sqlite3.Row) -> dict:
    return dict(row)


def list_findings(*, status: str | None = None, run_id: str | None = None, limit: int | None = 50,
                  db_path=None) -> list[dict]:
    if status is not None and status not in STATUSES:
        raise ReviewError(f"status must be one of: {', '.join(STATUSES)}")
    sql, params = "SELECT * FROM review_findings WHERE 1=1", []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if run_id:
        sql += " AND run_id = ?"
        params.append(run_id)
    sql += " ORDER BY updated_at DESC, created_at DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    conn = connect(db_path)
    try:
        return [_finding(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def get_finding(ref: str, *, db_path=None) -> dict:
    """A finding by id or by key."""
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT * FROM review_findings WHERE id = ? OR key = ?", (ref, ref)).fetchone()
        if row is None:
            raise ReviewError(f"no finding {ref!r}", 1)
        return _finding(row)
    finally:
        conn.close()


def set_status(finding_id: str, status: str, *, db_path=None) -> dict:
    if status not in STATUSES:
        raise ReviewError(f"status must be one of: {', '.join(STATUSES)}")
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT id FROM review_findings WHERE id = ? OR key = ?",
                           (finding_id, finding_id)).fetchone()
        if row is None:
            raise ReviewError(f"no finding {finding_id!r}", 1)
        conn.execute("UPDATE review_findings SET status = ?, updated_at = ? WHERE id = ?",
                     (status, _now_iso(), row["id"]))
        conn.commit()
        return _finding(conn.execute("SELECT * FROM review_findings WHERE id = ?", (row["id"],)).fetchone())
    finally:
        conn.close()


def latest_run(*, db_path=None) -> dict:
    """The newest run with the findings it raised or re-raised."""
    conn = connect(db_path)
    try:
        run = conn.execute("SELECT * FROM review_runs ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
        if run is None:
            raise ReviewError("no review has run yet", 1)
        found = [_finding(r) for r in conn.execute(
            "SELECT * FROM review_findings WHERE run_id = ? ORDER BY created_at, rowid", (run["id"],))]
        return {**dict(run), "findings": found}
    finally:
        conn.close()


def counts(*, db_path=None) -> dict:
    conn = connect(db_path)
    try:
        c = {s: 0 for s in STATUSES}
        for r in conn.execute("SELECT status, COUNT(*) n FROM review_findings GROUP BY status"):
            c[r["status"]] = r["n"]
        last = conn.execute("SELECT MAX(created_at) FROM review_runs").fetchone()[0]
        return {**c, "last_run_at": last}
    finally:
        conn.close()


def past_findings(*, days: int = PAST_FINDINGS_DAYS, limit: int = 40, today: date | None = None,
                  db_path=None) -> list[dict]:
    """Earlier findings and what the user did with them, so a run doesn't repeat itself and dismissals guide it."""
    since = (_today(today) - timedelta(days=days)).isoformat()
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT key, domain, title, status, times_raised, created_at, updated_at FROM review_findings "
            "WHERE updated_at >= ? ORDER BY updated_at DESC LIMIT ?", (since, limit)).fetchall()
        return [{**dict(r), "created_at": r["created_at"][:10], "updated_at": r["updated_at"][:10]} for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# gather: the cross-domain digest
# ---------------------------------------------------------------------------


def _sec_calendar(db_path, today: date) -> dict:
    conn = connect(db_path)
    try:
        end = (today + timedelta(days=14)).isoformat()
        rows = [dict(r) for r in conn.execute(
            "SELECT day, start_time, end_time, title, source FROM hub_items WHERE kind='event' AND day BETWEEN ? AND ? "
            "ORDER BY day, COALESCE(start_time,'00:00'), title", (today.isoformat(), end))]
        synced = conn.execute("SELECT MAX(synced_at) FROM hub_items WHERE kind='event'").fetchone()[0]
    finally:
        conn.close()
    by_day: dict[str, int] = {}
    for r in rows:
        by_day[r["day"]] = by_day.get(r["day"], 0) + 1
    overlaps = []
    for day in by_day:
        timed = [r for r in rows if r["day"] == day and r["start_time"] and r["end_time"]]
        for i, a in enumerate(timed):
            for b in timed[i + 1:]:
                if b["start_time"] < a["end_time"] and a["start_time"] < b["end_time"] and a["title"] != b["title"]:
                    overlaps.append({"day": day, "a": _clip(a["title"], 50), "b": _clip(b["title"], 50)})
    return {"synced_at": synced, "events_next_14d": len(rows), "events_per_day": by_day,
            "events": [{"day": r["day"], "time": (r["start_time"] or "all day"), "title": _clip(r["title"], 70),
                        "calendar": _clip(r["source"], 30)} for r in rows[:40]],
            "overlapping_timed_events": overlaps[:6]}


def _sec_tasks(db_path, today: date) -> dict:
    conn = connect(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT day, title, source, priority FROM hub_items WHERE kind='task' ORDER BY day, "
            "COALESCE(priority,4), title")]
        synced = conn.execute("SELECT MAX(synced_at) FROM hub_items WHERE kind='task'").fetchone()[0]
    finally:
        conn.close()
    t, soon = today.isoformat(), (today + timedelta(days=7)).isoformat()
    overdue = [r for r in rows if r["day"] < t]

    def brief(r):
        return {"due": r["day"], "title": _clip(r["title"], 70), "project": _clip(r["source"], 30),
                "priority": r["priority"]}

    oldest = min((r["day"] for r in overdue), default=None)
    return {"synced_at": synced, "open_with_due_date": len(rows), "overdue_count": len(overdue),
            "oldest_overdue_days": (today - date.fromisoformat(oldest)).days if oldest else None,
            "overdue": [brief(r) for r in overdue[:15]],
            "due_next_7_days": [brief(r) for r in rows if t <= r["day"] <= soon][:15]}


def _sec_money(db_path, today: date) -> dict:
    import money
    o = money.overview(db_path, today)
    sync = o.get("sync") or {}
    out = {
        "sync": {k: sync.get(k) for k in ("has_data", "last_ok_at", "age_minutes", "stale")},
        "month_elapsed_pct": o.get("month_elapsed_pct"),
        "budget": [{k: b.get(k) for k in ("label", "planned", "actual", "left", "pct", "status")}
                   for b in (o.get("budget") or [])][:8],
        "projection": o.get("projection"),
        "spending": o.get("spending"),
        "savings": o.get("savings"),
        "accounts": o.get("accounts"),
        "over_budget": [{"name": r.get("name"), "over": r.get("over")} for r in (o.get("over_budget") or [])],
        "retirement": o.get("retirement"),
        "review_queue": {k: (o.get("review") or {}).get(k) for k in ("needs_review", "no_who")},
    }
    nw = o.get("networth")
    if nw:
        out["networth"] = {k: nw.get(k) for k in ("value", "as_of", "change_30d", "change_ytd")}
    bills = o.get("bills") or {}
    items = bills.get("items") or []
    out["upcoming_bills"] = {"count": len(items), "total": bills.get("total"),
                             "next": [{"due": b["due_date"], "merchant": _clip(b["merchant"], 40), "amount": b["amount"]}
                                      for b in items[:8]]}
    return out


def _sec_wellness(db_path, today: date) -> dict:
    import insights
    import wellness
    now = datetime.combine(today, datetime.now().time())
    v = wellness.today_view(now=now, db_path=db_path)
    out = {
        "rings": {k: {f: r.get(f) for f in ("value", "zone", "hours", "note") if f in r}
                  for k, r in (v.get("rings") or {}).items()},
        "vitals": [{k: x.get(k) for k in ("label", "value", "unit", "status", "day")} for x in (v.get("vitals") or [])],
        "out_of_range": v.get("out_of_range"),
        "sleep_avg_hours_30d": (v.get("sleep") or {}).get("avg_hours_30"),
        "sleep_target_hours": (v.get("sleep") or {}).get("target_hours"),
        "source_freshness": v.get("freshness"),
    }
    try:
        tr = wellness.trends(30, now=now, db_path=db_path)
        out["trend_30d"] = {k: s.get("summary") for k, s in (tr.get("series") or {}).items()
                            if k in ("sleep_hours", "sleep_score", "hrv", "resting_hr", "stress", "steps", "rating",
                                     "load_ratio", "fatigue")}
        out["bedtime_sd_min"] = (tr.get("sleep") or {}).get("bedtime_sd_min")
        out["rating_by_weekday"] = [{"day": r["day"], "avg": r["avg"], "n": r["n"]}
                                    for r in (tr.get("rating_by_weekday") or [])]
    except Exception as exc:  # trends are an extra; the rest of the section still stands
        out["trend_30d_error"] = f"{type(exc).__name__}: {exc}"
    rep = insights.latest_report(db_path=db_path)
    if rep:
        out["latest_insights_report"] = {"written": rep["created_at"][:10], "narrative": _clip(rep["narrative"], 1500)}
    return out


def _sec_habits(db_path, today: date) -> dict:
    import habits
    items = habits.list_status(db_path=db_path, today=today)
    out = []
    for h in items:
        gap = (today - date.fromisoformat(h["last_done"])).days if h.get("last_done") else None
        out.append({"name": _clip(h["name"], 40), "owner": h.get("owner"), "frequency": h["frequency"],
                    "target": h.get("target_count"), "status": h["period_status"], "streak": h["current_streak"],
                    "longest": h["longest_streak"], "at_risk": h["at_risk"], "days_since_last": gap,
                    "total": h["total_count"]})
    return {"active": len(out), "at_risk": sum(1 for h in out if h["at_risk"]), "habits": out[:25]}


def _sec_chores(db_path, today: date) -> dict:
    import chores
    return {"week": [{k: w.get(k) for k in ("kid", "owed", "earned_this_week", "chores_done_this_week", "best_streak")}
                     for w in chores.week_summary(db_path=db_path, today=today)],
            "open_today": [{"kid": o["kid"], "open": len(o["remaining"]), "done": o["done_count"],
                            "total": o["total_count"]} for o in chores.open_today(db_path=db_path, today=today)]}


def _sec_mood(db_path, today: date) -> dict:
    import dayrating
    import journal
    st = dayrating.status(db_path=db_path, today=today)
    recent = dayrating.list_ratings(days=14, db_path=db_path, today=today)
    jh = journal.history(days=28, db_path=db_path, today=today)
    return {"rating": {k: st.get(k) for k in ("avg_7", "avg_30", "total_ratings", "logging_streak", "rated_today")},
            "recent_ratings": [{"day": r.get("rating_date"), "score": r.get("score"),
                                "note": _clip(r.get("note"), 90)} for r in recent][:14],
            "journal": {"answered_days_28": jh.get("answered_days"),
                        "questions": [{"q": _clip(q.get("label") or q.get("text"), 40), "yes": q.get("yes"),
                                       "no": q.get("no")} for q in (jh.get("questions") or []) if q.get("active")]}}


def _sec_home(db_path, today: date) -> dict:
    conn = connect(db_path)
    try:
        items = [dict(r) for r in conn.execute("""
            SELECT i.name, i.category, i.interval_days, i.created_at,
                   (SELECT MAX(completed_date) FROM home_maintenance_completions c WHERE c.item_id = i.id) AS last_done
              FROM home_maintenance_items i WHERE i.active = 1""")]
    finally:
        conn.close()
    out = []
    for it in items:
        base = date.fromisoformat((it["last_done"] or it["created_at"])[:10])
        due = base + timedelta(days=it["interval_days"])
        out.append({"name": _clip(it["name"], 50), "category": it["category"], "due": due.isoformat(),
                    "days_until_due": (due - today).days, "never_done": it["last_done"] is None})
    out.sort(key=lambda x: x["days_until_due"])
    return {"active_items": len(out), "overdue": [x for x in out if x["days_until_due"] < 0][:10],
            "due_within_30_days": [x for x in out if 0 <= x["days_until_due"] <= 30][:10]}


def _sec_lawn(db_path, today: date) -> dict:
    conn = connect(db_path)
    try:
        program = [dict(r) for r in conn.execute(
            "SELECT round_number, timing_month, task_label FROM lawn_garden_program ORDER BY round_number")]
        done = {r[0] for r in conn.execute(
            "SELECT DISTINCT round_number FROM lawn_garden_treatments WHERE treatment_date LIKE ? AND round_number IS NOT NULL",
            (f"{today.year}%",))}
        last = conn.execute("SELECT MAX(treatment_date) FROM lawn_garden_treatments").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM lawn_garden_issues WHERE status = 'active'").fetchone()[0]
        out_of_stock = conn.execute("SELECT COUNT(*) FROM lawn_garden_products WHERE in_stock = 0").fetchone()[0]
    finally:
        conn.close()
    missed = [p for p in program if p["round_number"] not in done and p["timing_month"] < today.month]
    due = [p for p in program if p["round_number"] not in done and p["timing_month"] == today.month]
    upcoming = [p for p in program if p["round_number"] not in done and p["timing_month"] > today.month][:1]
    brief = lambda ps: [{"round": p["round_number"], "name": _clip(p["task_label"], 40), "month": p["timing_month"]} for p in ps]  # noqa: E731
    return {"rounds_done_this_year": len(done), "rounds_total": len(program), "missed": brief(missed),
            "due_this_month": brief(due), "next_upcoming": brief(upcoming), "last_treatment": last,
            "active_weed_issues": active, "products_out_of_stock": out_of_stock}


_DATE_RE = re.compile(r"^(\d{4}-)?\d{2}-\d{2}$")


def _next_occurrence(md: str, today: date) -> date | None:
    try:
        m, d = int(md[-5:-3]), int(md[-2:])
        for year in (today.year, today.year + 1):
            try:
                cand = date(year, m, d)
            except ValueError:  # Feb 29
                continue
            if cand >= today:
                return cand
    except ValueError:
        return None
    return None


def _sec_dates(db_path, today: date) -> dict:
    """Names and dates only. No notes, sizes, allergies or other profile values."""
    conn = connect(db_path)
    try:
        people = [dict(r) for r in conn.execute("SELECT name, birthday FROM profile_people WHERE birthday IS NOT NULL")]
        facts = [dict(r) for r in conn.execute(
            "SELECT key, value FROM profile_facts WHERE lower(key) LIKE '%anniversary%' OR lower(key) LIKE '%birthday%'")]
    finally:
        conn.close()
    upcoming = []
    for p in people:
        nxt = _next_occurrence(str(p["birthday"]).strip(), today)
        if nxt and (nxt - today).days <= 45:
            upcoming.append({"what": f"{p['name']} birthday", "date": nxt.isoformat(), "days": (nxt - today).days})
    for f in facts:
        value = str(f["value"]).strip()
        if _DATE_RE.match(value):
            nxt = _next_occurrence(value, today)
            if nxt and (nxt - today).days <= 45:
                upcoming.append({"what": _clip(f["key"].replace("_", " "), 40), "date": nxt.isoformat(),
                                 "days": (nxt - today).days})
    upcoming.sort(key=lambda x: x["days"])
    return {"people_on_file": len(people), "upcoming_45_days": upcoming[:12]}


def _sec_scheduled(db_path, today: date) -> dict:
    conn = connect(db_path)
    try:
        tasks = [dict(r) for r in conn.execute(
            "SELECT id, name, cron_expression, enabled FROM scheduled_tasks ORDER BY name")]
        since = (today - timedelta(days=7)).isoformat()
        runs = [dict(r) for r in conn.execute(
            "SELECT task_id, run_at, status, summary FROM scheduled_task_runs WHERE run_at >= ? ORDER BY run_at DESC",
            (since,))]
        last_any = {r["task_id"]: r["run_at"] for r in conn.execute(
            "SELECT task_id, MAX(run_at) run_at FROM scheduled_task_runs GROUP BY task_id")}
    finally:
        conn.close()
    out, failures = [], []
    for t in tasks:
        mine = [r for r in runs if r["task_id"] == t["id"]]
        done = [r for r in mine if r["status"] != "dispatched"]
        bad = [r for r in done if r["status"] in ("failed", "dispatch_failed")]
        out.append({"name": t["name"], "cron": t["cron_expression"], "enabled": bool(t["enabled"]),
                    "runs_7d": len(done), "failed_7d": len(bad), "degraded_7d": sum(1 for r in done if r["status"] == "degraded"),
                    "last_run": (last_any.get(t["id"]) or "never")[:16]})
        failures += [{"task": t["name"], "at": r["run_at"][:16], "summary": _clip(r["summary"], 120)} for r in bad]
    return {"tasks": out[:40], "recent_failures": failures[:10]}


def _sec_system(db_path, today: date) -> dict:
    import system_health
    rows = system_health.get_status(db_path=db_path)
    return {"integrations": [{"key": r["key"], "status": r["status"],
                              **({"detail": _clip(r["detail"], 100)} if r["status"] != "ok" else {}),
                              "last_ok": (r.get("last_ok_at") or "")[:10] or None} for r in rows]}


def _sec_agent_results(db_path, today: date) -> dict:
    since = (today - timedelta(days=7)).isoformat()
    marks = ",".join("?" for _ in SENSITIVE_AGENTS)
    conn = connect(db_path)
    try:
        by_agent = {r["agent"]: r["n"] for r in conn.execute(
            f"SELECT agent, COUNT(*) n FROM agent_results WHERE created_at >= ? AND agent NOT IN ({marks}) "
            "GROUP BY agent ORDER BY n DESC", (since, *SENSITIVE_AGENTS))}
        recent = [dict(r) for r in conn.execute(
            f"SELECT agent, created_at, summary FROM agent_results WHERE created_at >= ? AND agent NOT IN ({marks}) "
            "ORDER BY created_at DESC LIMIT 15", (since, *SENSITIVE_AGENTS))]
    finally:
        conn.close()
    return {"results_7d_by_agent": by_agent,
            "recent": [{"agent": r["agent"], "at": r["created_at"][:16], "summary": _clip(r["summary"], 140)}
                       for r in recent]}


SECTIONS = {
    "calendar": _sec_calendar, "tasks": _sec_tasks, "money": _sec_money, "wellness": _sec_wellness,
    "habits": _sec_habits, "chores": _sec_chores, "mood": _sec_mood, "home": _sec_home, "lawn": _sec_lawn,
    "profile_dates": _sec_dates, "scheduled_tasks": _sec_scheduled, "system": _sec_system,
    "agent_results": _sec_agent_results,
}


def _shrink(obj, items: int, text: int, depth: int = 0):
    """Truncate every list, long string and (below the section level) wide dict, recursively, noting how many
    entries were dropped."""
    if isinstance(obj, dict):
        keys = list(obj)
        if depth >= 3 and len(keys) > items:
            kept = {k: _shrink(obj[k], items, text, depth + 1) for k in keys[:items]}
            kept["_more"] = f"{len(keys) - items} more"
            return kept
        return {k: _shrink(v, items, text, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        kept = [_shrink(v, items, text, depth + 1) for v in obj[:items]]
        if len(obj) > items:
            kept.append(f"... {len(obj) - items} more")
        return kept
    if isinstance(obj, str) and len(obj) > text:
        return obj[: text - 1] + "…"
    return obj


def _size(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def gather(*, db_path=None, today: date | None = None, max_chars: int = MAX_CHARS) -> dict:
    """The cross-domain digest the review agent reasons over. Never raises for a bad section."""
    today = _today(today)
    db_path = Path(db_path) if db_path else DB_PATH  # one database for every section
    sections = {}
    for name, fn in SECTIONS.items():
        try:
            sections[name] = fn(db_path, today)
        except Exception as exc:  # one broken area must not stop the review
            sections[name] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        past = past_findings(today=today, db_path=db_path)
        last = counts(db_path=db_path)
    except Exception as exc:
        past, last = {"error": f"{type(exc).__name__}: {exc}"}, {}
    digest = {"today": today.isoformat(), "weekday": today.strftime("%A"), "generated_at": _now_iso(),
              "failed_sections": [n for n, s in sections.items() if "error" in s],
              "previous_findings": {"last_run_at": last.get("last_run_at"),
                                    "open_counts": {k: last.get(k) for k in STATUSES},
                                    "findings": past},
              "sections": sections}
    for items, text in ((25, 300), (15, 200), (8, 150), (4, 100), (2, 60)):
        if _size(digest) <= max_chars:
            break
        digest = _shrink(digest, items, text)
    # Last resort: drop the largest sections whole (named, so the agent knows what is missing).
    while _size(digest) > max_chars:
        name = max(digest["sections"], key=lambda n: _size(digest["sections"][n]))
        if _size(digest["sections"][name]) <= 40:
            break
        digest["sections"][name] = {"truncated": "left out to keep the digest under the size limit"}
    digest["digest_chars"] = _size(digest)
    return digest
