"""System: logins that died and scheduled tasks that failed, so a silent break can't hide."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROVIDER = {"name": "system", "title": "System"}
LABELS = {"hyvee": "Hy-Vee", "drive": "Google Drive", "monarch": "Monarch", "coros": "COROS"}


def collect(conn, today):
    db = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    attention, creds = [], []
    try:
        data = json.loads((db.parent / "credential_check.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        data = {}
    for key, c in data.items():
        label = LABELS.get(key, key)
        creds.append({"key": key, "label": label, "status": c.get("status")})
        if c.get("status") == "dead":
            attention.append({"key": f"cred:{key}:{c.get('authed_at')}", "href": "/health/system",
                              "level": "warn", "text": f"{label} login has expired"})
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
    fails = conn.execute(
        "SELECT t.name, r.run_at FROM scheduled_task_runs r JOIN scheduled_tasks t ON t.id=r.task_id "
        "WHERE r.status IN ('failed','dispatch_failed') AND r.run_at>=? ORDER BY r.run_at DESC", (cutoff,)).fetchall()
    for f in fails:
        attention.append({"key": f"sched:{f['name']}:{f['run_at']}", "href": "/health/schedule", "level": "warn",
                          "text": f"Scheduled task {f['name']} failed"})
    return {"card": {"logins": creds, "failed_24h": len(fails)}, "attention": attention}
