"""Operational status readers: login expiry, backups, scheduler heartbeat (read-only).
Backup archives are only listed (name/size/mtime) — never opened."""
import os
from datetime import datetime, timedelta
from pathlib import Path

from . import common

REMIND_DAYS_BEFORE = 2  # matches scripts/credential_check.py
HEARTBEAT_STALE_MIN = 20  # matches scripts/watchdog_scheduler_health.py
LABELS = {"hyvee": "Hy-Vee", "drive": "Google Drive", "monarch": "Monarch"}


def _local_aware(value):
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt.astimezone()  # naive datetimes are treated as local wall-clock


def get_credentials():
    raw = common.read_json(common.state_dir() / "credential_check.json")
    if not isinstance(raw, dict):
        return {"error": "credential_check.json not found", "systems": []}
    now = datetime.now().astimezone()
    systems = []
    for key, entry in raw.items():
        authed = _local_aware(entry.get("authed_at")) if isinstance(entry, dict) else None
        lifetime = (entry or {}).get("lifetime_days") if isinstance(entry, dict) else None
        if not authed or not lifetime:
            systems.append({"key": key, "label": LABELS.get(key, key), "state": "unknown"})
            continue
        expires = authed + timedelta(days=float(lifetime))
        days_left = (expires - now).total_seconds() / 86400
        state = "expired" if days_left < 0 else "warn" if days_left <= REMIND_DAYS_BEFORE else "ok"
        systems.append({
            "key": key, "label": LABELS.get(key, key), "state": state,
            "authed_at": authed.isoformat(), "expires_at": expires.isoformat(),
            "lifetime_days": float(lifetime), "days_left": round(days_left, 1),
            "age_days": round((now - authed).total_seconds() / 86400, 1),
            "probe_status": entry.get("status"),
        })
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
