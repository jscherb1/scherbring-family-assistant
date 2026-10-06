"""Scheduled-task listing for the dashboard (read-only)."""
import sqlite3
import sys
from datetime import datetime, timedelta

from . import common

DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
FAIL_STATUSES = ("failed", "dispatch_failed")


def _scheduler_store():
    """Import scripts/scheduler_store.py so cron matching is never reimplemented."""
    scripts = str(common.SCRIPTS_DIR)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import scheduler_store
    return scheduler_store


def _int_list(field: str, lo: int, hi: int):
    """Parse a plain list/range field ('1,4,7' or '1-5') into ints, else None.
    Steps and anything unusual return None so callers fall back to the raw cron."""
    values = []
    for part in field.split(","):
        if "/" in part or not part:
            return None
        try:
            if "-" in part:
                a, b = part.split("-", 1)
                values.extend(range(int(a), int(b) + 1))
            else:
                values.append(int(part))
        except ValueError:
            return None
    if any(v < lo or v > hi for v in values):
        return None
    return sorted(set(values))


def _time_text(hour: int, minute: int) -> str:
    suffix = "AM" if hour < 12 else "PM"
    h12 = hour % 12 or 12
    return f"{h12}:{minute:02d} {suffix}"


def describe_cron(expr: str) -> str:
    """Plain-English version of a 5-field cron expression; the raw expression when
    the pattern isn't one of the simple shapes we recognise."""
    fields = (expr or "").split()
    if len(fields) != 5:
        return expr
    minute_f, hour_f, dom_f, month_f, dow_f = fields
    if not (minute_f.isdigit() and hour_f.isdigit()):
        return expr
    minute, hour = int(minute_f), int(hour_f)
    if minute > 59 or hour > 23:
        return expr
    at = _time_text(hour, minute)

    months = None if month_f == "*" else _int_list(month_f, 1, 12)
    if month_f != "*" and months is None:
        return expr
    month_text = ""
    if months:
        month_text = " in " + ", ".join(MONTH_NAMES[m - 1] for m in months)

    if dom_f == "*" and dow_f == "*":
        return f"Daily at {at}{month_text}"

    if dom_f == "*":
        dows = _int_list(dow_f, 0, 7)
        if not dows:
            return expr
        dows = sorted({d % 7 for d in dows})
        if len(dows) == 1:
            days = DAY_NAMES[dows[0]] + "s"
        else:
            days = ", ".join(DAY_NAMES[d][:3] for d in dows)
        return f"{days} at {at}{month_text}"

    if dow_f == "*":
        doms = _int_list(dom_f, 1, 31)
        if not doms:
            return expr
        which = ", ".join(str(d) for d in doms)
        return f"Day {which} of the month at {at}{month_text}"

    return expr


def _next_run(cron_expression: str, after: datetime):
    store = _scheduler_store()
    try:
        return next(store.iter_matches_after(cron_expression, after), None)
    except (store.CronError, ValueError):
        return None


def list_tasks(now: datetime | None = None):
    """All scheduled tasks with next-run, 7-day stats and recent runs.

    Timestamps in the DB are naive local wall-clock strings, so `now` is naive local too.
    Returns {"tasks": [...]} or {"error": "..."}.
    """
    now = now or datetime.now()
    since = (now - timedelta(days=7)).isoformat(timespec="seconds")
    try:
        conn = common.connect_ro()
    except (FileNotFoundError, sqlite3.OperationalError) as exc:
        return {"error": str(exc)}

    try:
        tasks = conn.execute(
            "SELECT id, name, prompt, cron_expression, enabled, last_dispatched_at, created_at "
            "FROM scheduled_tasks"
        ).fetchall()
        runs = conn.execute(
            "SELECT task_id, run_at, dispatched_at, status, summary "
            "FROM scheduled_task_runs ORDER BY run_at DESC, id DESC"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        return {"error": f"query failed: {exc}"}
    finally:
        conn.close()

    by_task: dict[str, list] = {}
    for r in runs:
        by_task.setdefault(r["task_id"], []).append(r)

    result = []
    for t in tasks:
        task_runs = by_task.get(t["id"], [])
        recent = task_runs[:5]
        in_week = [r for r in task_runs if r["run_at"] and r["run_at"] >= since]
        enabled = bool(t["enabled"])
        next_run = _next_run(t["cron_expression"], now) if enabled else None
        result.append({
            "id": t["id"],
            "name": t["name"],
            "prompt": t["prompt"],
            "cron": t["cron_expression"],
            "schedule": describe_cron(t["cron_expression"]),
            "enabled": enabled,
            "next_run": next_run.isoformat(timespec="seconds") if next_run else None,
            "last_dispatched_at": t["last_dispatched_at"],
            "last_status": recent[0]["status"] if recent else None,
            "last_run_at": recent[0]["run_at"] if recent else None,
            "ok_7d": sum(1 for r in in_week if r["status"] == "ok"),
            "fail_7d": sum(1 for r in in_week if r["status"] in FAIL_STATUSES),
            "recent_runs": [
                {"run_at": r["run_at"], "status": r["status"], "summary": r["summary"]}
                for r in recent
            ],
        })

    # Enabled tasks by next run; disabled (or un-schedulable) last.
    result.sort(key=lambda x: (x["next_run"] is None, x["next_run"] or "", x["name"]))
    return {"tasks": result}
