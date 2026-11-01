"""Scheduled-task run health and scheduler watchdog readers."""
import sqlite3
from datetime import timedelta

from . import common
from .common import now, parse_ts, read_json


def get_task_run_health():
    if not common.db_path().exists():
        return {"error": "agent_results.db not found"}

    try:
        conn = common.connect_ro()
    except sqlite3.OperationalError as exc:
        return {"error": f"could not open db read-only: {exc}"}

    try:
        rows = conn.execute(
            """
            SELECT r.run_at, r.status, r.summary, t.name AS task_name
            FROM scheduled_task_runs r
            LEFT JOIN scheduled_tasks t ON t.id = r.task_id
            ORDER BY r.run_at DESC
            LIMIT 50
            """
        ).fetchall()
    except sqlite3.OperationalError as exc:
        conn.close()
        return {"error": f"query failed: {exc}"}

    conn.close()

    current = now()
    ok_24h = fail_24h = ok_7d = fail_7d = deg_24h = deg_7d = 0
    recent_failures = []
    recent_degraded = []

    for row in rows:
        ts = parse_ts(row["run_at"])
        is_fail = row["status"] in ("failed", "dispatch_failed")
        is_ok = row["status"] == "ok"
        is_deg = row["status"] == "degraded"

        if ts and current - ts <= timedelta(hours=24):
            if is_ok:
                ok_24h += 1
            elif is_deg:
                deg_24h += 1
            elif is_fail:
                fail_24h += 1
        if ts and current - ts <= timedelta(days=7):
            if is_ok:
                ok_7d += 1
            elif is_deg:
                deg_7d += 1
            elif is_fail:
                fail_7d += 1

        if is_fail and len(recent_failures) < 10:
            recent_failures.append({
                "task_name": row["task_name"] or "(unknown task)",
                "run_at": row["run_at"],
                "status": row["status"],
                "summary": row["summary"],
            })

        if is_deg and len(recent_degraded) < 10:
            recent_degraded.append({
                "task_name": row["task_name"] or "(unknown task)",
                "run_at": row["run_at"],
                "status": row["status"],
                "summary": row["summary"],
            })

    # degraded runs finished and delivered, so they are not failures in the rate
    total_24h = ok_24h + deg_24h + fail_24h
    success_rate_24h = ((ok_24h + deg_24h) / total_24h * 100) if total_24h else None

    return {
        "ok_24h": ok_24h,
        "fail_24h": fail_24h,
        "ok_7d": ok_7d,
        "fail_7d": fail_7d,
        "degraded_24h": deg_24h,
        "degraded_7d": deg_7d,
        "success_rate_24h": success_rate_24h,
        "recent_failures": recent_failures,
        "recent_degraded": recent_degraded,
    }


def get_watchdog_status():
    result = {}
    state = common.state_dir()
    for name, path in (("scheduler", state / "scheduler_watchdog.json"),):
        data = read_json(path)
        if data is None:
            result[name] = {"error": "watchdog state file not found"}
            continue
        result[name] = {
            "healthy": data.get("first_unhealthy_at") is None,
            "first_unhealthy_at": data.get("first_unhealthy_at"),
            "last_restart_at": data.get("last_restart_at"),
        }
    return result


def get_status():
    """Full payload for /api/status (shape unchanged from the Flask PoC)."""
    return {
        "generated_at": now().isoformat(),
        "task_runs": get_task_run_health(),
        "watchdogs": get_watchdog_status(),
    }
