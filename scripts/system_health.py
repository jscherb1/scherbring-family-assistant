"""System health service: is each integration actually working right now?

Zero-dependency (Python stdlib only), same SQLite file as the rest of the assistant. The hub's Systems tile
and the agent CLI (`system_health_store.py`) both call this module, so they always agree.

Two kinds of system
  - script-side: SCRIPT_CHECKS run here (token refresh, a cheap HTTP read, a saved-session look). They never
    log in again, so a check can't sign you out of anything.
  - connector-side (Todoist, Google Calendar, Gmail, Home Assistant): only an agent can reach them, so
    it calls record() with the result of one cheap read. Until then the tile says "not checked yet".
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import dayrating  # noqa: E402

STATUSES = ("ok", "warn", "down", "unknown")
# key -> (label, where it's checked)
SYSTEMS = {
    "coros_mcp": ("COROS (health)", "script"),
    "coros_web": ("COROS (training hub)", "script"),
    "open_meteo": ("Open-Meteo weather", "script"),
    "drive": ("Google Drive", "script"),
    "monarch": ("Monarch", "script"),
    "money_sync": ("Monarch snapshot sync", "script"),
    "hyvee": ("Hy-Vee", "script"),
    "todoist": ("Todoist", "agent"),
    "calendar": ("Google Calendar", "agent"),
    "gmail": ("Gmail", "agent"),
    "home_assistant": ("Home Assistant", "agent"),
}
# Hub snapshot freshness stands in for a live ping on these until an agent records one.
SNAPSHOT_FALLBACK = {"todoist": "task", "calendar": "event"}
RECONNECT_HINTS = {
    "drive": " Fix it from your phone: reply \"reconnect drive\" here, or open Reconnect on the dashboard.",
}
STALE_HOURS = 24
PING_STALE_HOURS = 30  # an agent-recorded result older than this means the twice-daily ping stopped running
DOWN_AFTER = 2  # consecutive failures before a check is called "down" (one blip is only "warn")


class HealthError(Exception):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.message = message
        self.code = code


def connect(db_path=None) -> sqlite3.Connection:
    return dayrating.connect(db_path)


def _now() -> datetime:
    return datetime.now().astimezone()


def telegram_notify(message: str) -> None:
    """Best-effort Telegram alert via the direct Bot API script; never raises (an alert failure must not hide a check)."""
    import subprocess
    try:
        subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "telegram_send.py"), message],
                       timeout=30, check=False, capture_output=True)
    except Exception:  # noqa: BLE001
        pass


def record(key: str, status: str, detail: str = "", *, db_path=None, now: datetime | None = None,
           notify=None) -> dict:
    """Save one check result. A single failure is "warn"; DOWN_AFTER in a row is "down".

    `notify(message)` is called only on a state change into "down" or back from "down" to "ok", so a system that
    stays down alerts once, not every check."""
    if key not in SYSTEMS:
        raise HealthError(f"unknown system {key!r}; known: {', '.join(SYSTEMS)}")
    if status not in STATUSES:
        raise HealthError(f"status must be one of {', '.join(STATUSES)}")
    now_s = (now or _now()).isoformat(timespec="seconds")
    conn = connect(db_path)
    try:
        prev = conn.execute("SELECT * FROM system_health WHERE key=?", (key,)).fetchone()
        prev_status = prev["status"] if prev else None
        streak = prev["fail_streak"] if prev else 0
        last_ok = prev["last_ok_at"] if prev else None
        if status == "ok":
            streak, last_ok = 0, now_s
        elif status in ("warn", "down"):
            streak += 1
            status = "down" if status == "down" or streak >= DOWN_AFTER else "warn"
        elif prev:  # "unknown" = an inconclusive probe: keep the last verdict, only refresh the timestamp
            status = prev["status"]
        with conn:
            conn.execute(
                "INSERT INTO system_health (key,status,detail,checked_at,last_ok_at,fail_streak) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET status=excluded.status, detail=excluded.detail, "
                "checked_at=excluded.checked_at, last_ok_at=excluded.last_ok_at, fail_streak=excluded.fail_streak",
                (key, status, detail, now_s, last_ok, streak))
    finally:
        conn.close()
    label = SYSTEMS[key][0]
    if notify and status == "down" and prev_status != "down":
        hint = RECONNECT_HINTS.get(key, "")
        notify(f"🔴 {label} is down: {detail}{hint}")
    elif notify and status == "ok" and prev_status == "down":
        notify(f"🟢 {label} is working again.")
    return {"key": key, "status": status, "detail": detail, "checked_at": now_s, "fail_streak": streak}


# ---- script-side checks: each returns (status, detail) ----

def check_coros_mcp():
    import fitness.coros_mcp as cm  # access_token() refreshes the token when it is near expiry
    try:
        cm.access_token()
        return "ok", "token valid"
    except cm.CorosMcpError as e:
        if "not signed in" in e.message or "sign in again" in e.message or "invalid_grant" in e.message:
            return "down", "needs sign-in: python scripts/fitness/coros_mcp.py auth-url"
        return "warn", e.message[:160]


def check_coros_web():
    f = REPO_ROOT / "state" / "coros_session.json"
    if not f.exists():
        return "warn", "no saved session (the next planner run will log in)"
    age_days = (time.time() - f.stat().st_mtime) / 86400
    # Not re-verified on purpose: COROS allows one web session, so a login would sign you out elsewhere.
    return "ok", f"saved session {age_days:.1f} days old (not re-verified)"


def check_open_meteo():
    url = "https://api.open-meteo.com/v1/forecast?latitude=44&longitude=-92&current=temperature_2m"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return ("ok", "reachable") if r.status == 200 else ("warn", f"HTTP {r.status}")
    except Exception as e:  # noqa: BLE001
        return "warn", f"unreachable: {e}"[:160]


def _from_probe(fn):
    s = fn()
    return {"ok": ("ok", "live read worked"),
            "dead": ("down", "login expired; the System page has the re-login command")}.get(
        s, ("unknown", "probe inconclusive"))


def check_drive():
    import credential_check as cc
    return _from_probe(cc.probe_drive)


def check_monarch():
    import credential_check as cc
    return _from_probe(cc.probe_monarch)


def check_money_sync():
    import money
    st = money.sync_status()
    last = st["last_run"] or {}
    if not st["has_data"]:
        return "warn", "no successful sync yet" + (f" (last: {last.get('status')})" if last else "")
    if last.get("status") == "auth_failed":
        # "warn", not "down": the Monarch check already alerts on an expired login.
        return "warn", "Monarch login expired; the money pages show the last good snapshot"
    hour = datetime.now().hour
    if st["stale"] and 7 <= hour <= 23:  # syncs run 6am-11pm; overnight age is expected
        return "warn", f"last good sync {st['age_minutes']} min ago" + (f" ({last.get('status')}: {last.get('error')})"[:160] if last.get("error") else "")
    return "ok", f"synced {st['age_minutes']} min ago"


def check_hyvee():
    # The one Hy-Vee check lives in scripts/hyvee/hyvee_connection.py (also keeps the saved session fresh).
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "hyvee"))
    import hyvee_connection as hc
    return hc.health_detail(hc.check())


SCRIPT_CHECKS = {"coros_mcp": check_coros_mcp, "coros_web": check_coros_web, "open_meteo": check_open_meteo,
                 "drive": check_drive, "monarch": check_monarch, "money_sync": check_money_sync, "hyvee": check_hyvee}


def run_checks(keys=None, *, db_path=None, notify=None) -> list[dict]:
    out = []
    for key in keys or SCRIPT_CHECKS:
        if key not in SCRIPT_CHECKS:
            raise HealthError(f"{key!r} has no script-side check (agent-recorded systems use `record`)")
        try:
            status, detail = SCRIPT_CHECKS[key]()
        except Exception as e:  # noqa: BLE001 - a crashing check is a finding, not a crash
            status, detail = "warn", f"check crashed: {e}"[:160]
        out.append(record(key, status, detail, db_path=db_path, notify=notify))
    return out


def get_status(*, db_path=None, now: datetime | None = None) -> list[dict]:
    """Every known system with its latest result. Never omits one: an unchecked system says so."""
    now = (now or _now()).astimezone()
    conn = connect(db_path)
    try:
        rows = {r["key"]: dict(r) for r in conn.execute("SELECT * FROM system_health")}
        snaps = {r["kind"]: r["at"] for r in conn.execute(
            "SELECT kind, MAX(synced_at) at FROM hub_items GROUP BY kind")}
    finally:
        conn.close()
    out = []
    for key, (label, where) in SYSTEMS.items():
        r = rows.get(key)
        item = {"key": key, "label": label, "where": where, "status": "unknown",
                "detail": "not checked yet", "checked_at": None, "last_ok_at": None}
        if r:
            item.update(status=r["status"], detail=r["detail"] or "", checked_at=r["checked_at"],
                        last_ok_at=r["last_ok_at"])
            if where == "agent" and r["status"] == "ok":
                age = (now - datetime.fromisoformat(r["checked_at"]).astimezone()).total_seconds() / 3600
                if age > PING_STALE_HOURS:
                    item.update(status="warn", detail=f"last ping {age:.0f}h ago (the agent ping stopped?)")
        elif key in SNAPSHOT_FALLBACK and snaps.get(SNAPSHOT_FALLBACK[key]):
            at = snaps[SNAPSHOT_FALLBACK[key]]
            age = (now.replace(tzinfo=None) - datetime.fromisoformat(at).replace(tzinfo=None)).total_seconds() / 3600
            item.update(status="ok" if age <= STALE_HOURS else "warn", checked_at=at,
                        detail=f"inferred from the hub sync {age:.0f}h ago")
        out.append(item)
    return out
