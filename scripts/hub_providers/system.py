"""System: every integration's live health plus failed scheduled tasks, so a silent break can't hide."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import system_health

PROVIDER = {"name": "system", "title": "System"}


def collect(conn, today):
    db = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    attention, systems = [], []
    for s in system_health.get_status(db_path=db):
        systems.append({"key": s["key"], "label": s["label"], "status": s["status"], "detail": s["detail"],
                        "checked_at": s["checked_at"]})
        if s["status"] == "down":
            attention.append({"key": f"sys:{s['key']}", "href": "/health/system", "level": "warn",
                              "text": f"{s['label']} is down: {s['detail']}"})
        elif s["status"] == "warn" and s["key"] in ("todoist", "calendar", "gmail", "home_assistant"):
            attention.append({"key": f"sys:{s['key']}", "href": "/health/system", "level": "info",
                              "text": f"{s['label']} data looks stale ({s['detail']})"})
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
    fails = conn.execute(
        "SELECT t.name, r.run_at FROM scheduled_task_runs r JOIN scheduled_tasks t ON t.id=r.task_id "
        "WHERE r.status IN ('failed','dispatch_failed') AND r.run_at>=? ORDER BY r.run_at DESC", (cutoff,)).fetchall()
    for f in fails:
        attention.append({"key": f"sched:{f['name']}:{f['run_at']}", "href": "/health/schedule", "level": "warn",
                          "text": f"Scheduled task {f['name']} failed"})
    return {"card": {"logins": systems, "failed_24h": len(fails)}, "attention": attention}
