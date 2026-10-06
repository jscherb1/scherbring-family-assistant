"""Orchestrator dashboard — read-only PoC web app.

Reads existing state from ../state/ (SQLite + JSON/JSONL files) and serves a
small status dashboard. Never writes to any of those files.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Flask, jsonify, render_template

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / "state"
DB_PATH = STATE_DIR / "agent_results.db"
RESTARTS_PATH = STATE_DIR / "orchestrator_restarts.jsonl"
TELEGRAM_WATCHDOG_PATH = STATE_DIR / "telegram_watchdog.json"
SCHEDULER_WATCHDOG_PATH = STATE_DIR / "scheduler_watchdog.json"
TELEGRAM_FALLBACK_PATH = STATE_DIR / "telegram_fallback_used.json"

app = Flask(__name__)


def _parse_ts(value):
    """Parse an ISO-8601 timestamp (with or without a trailing Z) to an aware datetime."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _now():
    return datetime.now(timezone.utc)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def get_uptime_info():
    if not RESTARTS_PATH.exists():
        return {"error": "no restart log found", "last_restart_at": None,
                "restarts_24h": 0, "restarts_7d": 0}

    now = _now()
    restarts = []
    with open(RESTARTS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = _parse_ts(row.get("ts"))
            if ts:
                restarts.append(ts)

    if not restarts:
        return {"error": "restart log empty", "last_restart_at": None,
                "restarts_24h": 0, "restarts_7d": 0}

    restarts.sort()
    last_restart = restarts[-1]
    uptime_seconds = (now - last_restart).total_seconds()
    restarts_24h = sum(1 for ts in restarts if now - ts <= timedelta(hours=24))
    restarts_7d = sum(1 for ts in restarts if now - ts <= timedelta(days=7))

    return {
        "last_restart_at": last_restart.isoformat(),
        "uptime_seconds": max(uptime_seconds, 0),
        "restarts_24h": restarts_24h,
        "restarts_7d": restarts_7d,
    }


def get_task_run_health():
    if not DB_PATH.exists():
        return {"error": "agent_results.db not found"}

    try:
        uri = f"file:{DB_PATH.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
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

    now = _now()
    ok_24h = fail_24h = ok_7d = fail_7d = 0
    recent_failures = []

    for row in rows:
        ts = _parse_ts(row["run_at"])
        is_fail = row["status"] in ("failed", "dispatch_failed")
        is_ok = row["status"] == "ok"

        if ts and now - ts <= timedelta(hours=24):
            if is_ok:
                ok_24h += 1
            elif is_fail:
                fail_24h += 1
        if ts and now - ts <= timedelta(days=7):
            if is_ok:
                ok_7d += 1
            elif is_fail:
                fail_7d += 1

        if is_fail and len(recent_failures) < 10:
            recent_failures.append({
                "task_name": row["task_name"] or "(unknown task)",
                "run_at": row["run_at"],
                "status": row["status"],
                "summary": row["summary"],
            })

    total_24h = ok_24h + fail_24h
    success_rate_24h = (ok_24h / total_24h * 100) if total_24h else None

    return {
        "ok_24h": ok_24h,
        "fail_24h": fail_24h,
        "ok_7d": ok_7d,
        "fail_7d": fail_7d,
        "success_rate_24h": success_rate_24h,
        "recent_failures": recent_failures,
    }


def get_watchdog_status():
    result = {}
    for name, path in (("telegram", TELEGRAM_WATCHDOG_PATH), ("scheduler", SCHEDULER_WATCHDOG_PATH)):
        data = _read_json(path)
        if data is None:
            result[name] = {"error": "watchdog state file not found"}
            continue
        result[name] = {
            "healthy": data.get("first_unhealthy_at") is None,
            "first_unhealthy_at": data.get("first_unhealthy_at"),
            "last_restart_at": data.get("last_restart_at"),
        }
    return result


def get_telegram_fallback():
    data = _read_json(TELEGRAM_FALLBACK_PATH)
    if data is None:
        return {"last_used_at": None, "reason": None}
    return {"last_used_at": data.get("last_used_at"), "reason": data.get("reason")}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    return jsonify({
        "generated_at": _now().isoformat(),
        "uptime": get_uptime_info(),
        "task_runs": get_task_run_health(),
        "watchdogs": get_watchdog_status(),
        "telegram_fallback": get_telegram_fallback(),
    })


if __name__ == "__main__":
    # Localhost by default. To reach it over Tailscale, set DASHBOARD_HOST to
    # this machine's Tailscale IP (tailscale ip -4); avoid 0.0.0.0.
    app.run(
        host=os.environ.get("DASHBOARD_HOST", "127.0.0.1"),
        port=int(os.environ.get("DASHBOARD_PORT", "5151")),
        debug=False,
    )
