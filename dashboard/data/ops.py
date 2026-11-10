"""Operational status readers: login expiry, backups, scheduler heartbeat (read-only).
Backup archives are only listed (name/size/mtime) — never opened."""
import os
from datetime import datetime, timedelta
from pathlib import Path

from . import common

LOGIN_KEYS = ("drive", "monarch", "hyvee")
HEARTBEAT_STALE_MIN = 20  # matches scripts/watchdog_scheduler_health.py
LABELS = {"hyvee": "Hy-Vee", "drive": "Google Drive", "monarch": "Monarch"}


def _local_aware(value):
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt.astimezone()  # naive datetimes are treated as local wall-clock


def get_credentials():
    """Manually refreshed logins. State comes only from each system's last live check (scripts/system_health.py):
    ok = a real read worked, expired = a real read was rejected, unknown = not checked or inconclusive.
    Login age and guessed lifetimes are shown as information but never decide the state."""
    import sys
    sys.path.insert(0, str(common.SCRIPTS_DIR))
    sys.path.insert(0, str(common.SCRIPTS_DIR / "hyvee"))
    import hyvee_connection
    import system_health
    now = datetime.now().astimezone()
    rows = {s["key"]: s for s in system_health.get_status(db_path=common.db_path())}
    raw = common.read_json(common.state_dir() / "credential_check.json")
    raw = raw if isinstance(raw, dict) else {}
    systems = []
    for key in LOGIN_KEYS:
        row = rows.get(key, {})
        st = row.get("status")
        out = {"key": key, "label": LABELS[key], "state": {"ok": "ok", "warn": "warn", "down": "expired"}.get(st, "unknown"),
               "probe_status": st, "detail": row.get("detail"), "checked_at": row.get("checked_at")}
        if key == "hyvee":
            t = hyvee_connection.login_time()
        else:
            entry = raw.get(key)
            t = _local_aware((entry or {}).get("authed_at")) if isinstance(entry, dict) and entry.get("authed_at") else None
        if t:
            out.update(authed_at=t.isoformat(), age_days=round((now - t).total_seconds() / 86400, 1))
        systems.append(out)
    return {"systems": systems}


def _backup_dir() -> Path:
    return Path(os.environ.get("ASSISTANT_BACKUP_DIR") or Path.home() / "backups" / "assistant")


def get_backups():
    directory = _backup_dir()
    try:
        files = [f for f in directory.iterdir() if f.is_file()]
    except OSError:
        return {"error": "backup directory is not readable from here", "files": []}
    out = []
    for f in files:
        try:
            st = f.stat()
        except OSError:
            continue
        kind = "data" if f.name.startswith("data-") else "secrets" if f.name.startswith("secrets-") else "other"
        out.append({"name": f.name, "kind": kind, "size": st.st_size,
                    "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")})
    out.sort(key=lambda x: x["modified"], reverse=True)
    newest = {}
    for o in out:
        newest.setdefault(o["kind"], o)
    now = datetime.now()
    latest = out[0] if out else None
    age_h = round((now - datetime.fromisoformat(latest["modified"])).total_seconds() / 3600, 1) if latest else None
    return {
        "files": out[:20], "count": len(out), "total_size": sum(o["size"] for o in out),
        "newest_age_hours": age_h, "stale": age_h is not None and age_h > 36,
    }


def get_heartbeat():
    data = common.read_json(common.state_dir() / "scheduler_loop_state.json")
    tick = data.get("last_tick") if isinstance(data, dict) else None
    dt = _local_aware(tick) if tick else None
    if dt is None:
        return {"error": "no scheduler heartbeat recorded"}
    age = (datetime.now().astimezone() - dt).total_seconds() / 60
    return {"last_tick": tick, "age_minutes": round(age, 1), "stale": age > HEARTBEAT_STALE_MIN,
            "stale_after_minutes": HEARTBEAT_STALE_MIN}


def get_system():
    return {"credentials": get_credentials(), "backups": get_backups(), "heartbeat": get_heartbeat()}


@common.guarded
def get_integrations():
    """Every integration's last connection check (scripts/system_health.py), as the hub's Systems tile shows it."""
    import sys
    if str(common.SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(common.SCRIPTS_DIR))
    import system_health
    return {"systems": system_health.get_status(db_path=common.db_path())}
