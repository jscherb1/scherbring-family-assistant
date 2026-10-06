#!/usr/bin/env python3
"""Warn on Telegram before a manually refreshed login expires.

Some integrations cannot re-authenticate themselves: Hy-Vee (CAPTCHA), Google
Drive (consent), Monarch (cookie paste). This runs once a day from
systemd/assistant-credential-check.timer, costs no model tokens, and sends
direct Telegram messages so it still works if Claude itself is the broken piece.

Two layers per system:
  1. Advance reminder: once the login is older than (lifetime - 2 days) it
     reminds you daily, with the exact command, until you re-login.
  2. Live probe: checks the login really works right now. A dead login alerts
     immediately and shortens that system's lifetime estimate to what was
     observed, so the advance reminder gets better.

Lifetimes are estimates until observed. Edit them in state/credential_check.json
("lifetime_days") if you learn better. COROS is not listed: it re-logs in by
itself with the stored credentials.

Usage:
    python scripts/credential_check.py             # normal daily run
    python scripts/credential_check.py --status    # table only, nothing sent or saved
    python scripts/credential_check.py --dry-run   # show what would be sent, send nothing
    python scripts/credential_check.py --skip-probes
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from paths import REPO_ROOT, secrets_dir

STATE_FILE = REPO_ROOT / "state" / "credential_check.json"
HYVEE_SESSION = REPO_ROOT / "state" / "hyvee_session.json"
REMIND_DAYS_BEFORE = 2
ALERT_EVERY = timedelta(hours=20)  # at most one message per system per day


@dataclass(frozen=True)
class System:
    key: str
    label: str
    lifetime_days: float  # estimate until observed
    relogin: str  # what the user should do


SYSTEMS: dict[str, System] = {
    "hyvee": System(
        "hyvee", "Hy-Vee", 11,
        f"cd {REPO_ROOT} && .venv/bin/python scripts/hyvee/login_test.py --login-only --manual-wait 180\n"
        "(a browser window opens; solve the CAPTCHA if one shows)",
    ),
    "drive": System(
        "drive", "Google Drive", 7,
        f"cd {REPO_ROOT} && .venv/bin/python scripts/drive_upload.py auth\n"
        "(open the printed URL in your browser and approve)",
    ),
    "monarch": System(
        "monarch", "Monarch", 14,
        f"cd {REPO_ROOT}/vendor/monarch-mcp-server && .venv/bin/python login_setup.py\n"
        "(choose option 1 and paste the cookie header from app.monarch.com)",
    ),
}


# --------------------------------------------------------------------------- state


def _now() -> datetime:
    return datetime.now().astimezone()


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.astimezone()


def record_login(key: str, when: datetime | None = None) -> None:
    """Call right after a successful manual login. Never raises."""
    try:
        state = _load_state()
        entry = state.setdefault(key, {})
        entry["authed_at"] = (when or _now()).isoformat(timespec="seconds")
        entry.pop("last_alert_at", None)
        _save_state(state)
    except Exception:  # noqa: BLE001 - bookkeeping must never break a login
        pass


def _file_time(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    except OSError:
        return None


def authed_at(key: str, entry: dict) -> datetime | None:
    """When the current login was made, as best we know."""
    if key == "monarch":
        # login_setup.py writes this file once; normal use never rewrites it.
        seen = _file_time(Path.home() / ".monarch-mcp-server" / "token")
        if seen:
            return seen
    if entry.get("authed_at"):
        return _parse(entry["authed_at"])
    # First run, or a login we never saw: fall back to the credential file's time.
    fallback = {
        "hyvee": HYVEE_SESSION,
        "drive": secrets_dir() / "google" / "drive_token.json",
    }.get(key)
    return _file_time(fallback) if fallback else None


# -------------------------------------------------------------------------- probes
# Each returns "ok", "dead", or "unknown" (never treat a transient error as dead).


def probe_monarch() -> str:
    venv_python = REPO_ROOT / "vendor" / "monarch-mcp-server" / ".venv" / "bin" / "python"
    if not venv_python.exists():
        return "unknown"
    try:
        out = subprocess.run(
            [str(venv_python), str(REPO_ROOT / "scripts" / "monarch_probe.py")],
            capture_output=True, text=True, timeout=90,
        ).stdout.strip().splitlines()
        return json.loads(out[-1])["status"] if out else "unknown"
    except (subprocess.SubprocessError, ValueError, KeyError, IndexError):
        return "unknown"


def probe_drive() -> str:
    try:
        import drive_upload

        drive_upload._load_credentials(interactive=False)
        return "ok"
    except RuntimeError:
        return "dead"  # missing token or a rejected refresh token
    except Exception:  # noqa: BLE001 - network etc.
        return "unknown"


def probe_hyvee() -> str:
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "hyvee"))
    session = HYVEE_SESSION
    if not session.exists():
        return "dead"
    try:
        from playwright.sync_api import sync_playwright

        from login_test import is_logged_in

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            try:
                context = browser.new_context(storage_state=str(session), viewport={"width": 1366, "height": 900})
                return "ok" if is_logged_in(context.new_page()) else "dead"
            finally:
                browser.close()  # session state is deliberately not saved back
    except Exception:  # noqa: BLE001
        return "unknown"


PROBES: dict[str, Callable[[], str]] = {"hyvee": probe_hyvee, "drive": probe_drive, "monarch": probe_monarch}


# ------------------------------------------------------------------------- decision


def _fmt_days(days: float) -> str:
    return f"{days:.0f}" if abs(days - round(days)) < 0.25 else f"{days:.1f}"


def evaluate(
    system: System,
    entry: dict,
    probe_status: str,
    now: datetime,
    login_time: datetime | None,
) -> tuple[str | None, dict]:
    """Decide on a message for one system.

    Returns (message or None, updated entry). `entry` is not mutated.
    """
    entry = dict(entry)
    lifetime = float(entry.get("lifetime_days", system.lifetime_days))
    entry["lifetime_days"] = lifetime
    age_days = (now - login_time).total_seconds() / 86400 if login_time else None

    last_alert = _parse(entry["last_alert_at"]) if entry.get("last_alert_at") else None
    due_for_message = last_alert is None or now - last_alert >= ALERT_EVERY

    if probe_status == "dead":
        if age_days is not None and age_days < lifetime:
            # It died sooner than we assumed: learn from it.
            lifetime = max(1.0, round(age_days, 1))
            entry["lifetime_days"] = lifetime
        entry["status"] = "dead"
        if not due_for_message:
            return None, entry
        entry["last_alert_at"] = now.isoformat(timespec="seconds")
        since = f" (last login {_fmt_days(age_days)} days ago)" if age_days is not None else ""
        return (
            f"\U0001f534 {system.label} login has EXPIRED{since}. Anything that needs it will fail until you re-login:\n\n"
            f"{system.relogin}",
            entry,
        )

    entry["status"] = probe_status
    if age_days is None:
        return None, entry
    days_left = lifetime - age_days
    if days_left > REMIND_DAYS_BEFORE:
        entry.pop("last_alert_at", None)  # a fresh login re-arms the reminder
        return None, entry
    if not due_for_message:
        return None, entry
    entry["last_alert_at"] = now.isoformat(timespec="seconds")
    if days_left > 0:
        when = f"is expected to expire in about {_fmt_days(days_left)} day(s)"
    else:
        when = "is past its expected lifetime and may stop working any time"
    return (
        f"\U0001f7e1 {system.label} login {when} (last login {_fmt_days(age_days)} days ago, "
        f"assumed lifetime {_fmt_days(lifetime)} days). Refresh it when convenient:\n\n"
        f"{system.relogin}",
        entry,
    )


# ----------------------------------------------------------------------------- main


def send_telegram(text: str) -> bool:
    try:
        import telegram_send as ts

        token, chat_id = ts._read_token(), ts._default_chat_id()
        if not token or not chat_id:
            print("credential_check: no Telegram token/chat id configured", file=sys.stderr)
            return False
        ts._send_raw(token, chat_id, text)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"credential_check: Telegram send failed: {exc}", file=sys.stderr)
        return False


def run(
    probes: dict[str, Callable[[], str]] | None = None,
    now: datetime | None = None,
    send: Callable[[str], bool] = send_telegram,
    dry_run: bool = False,
    status_only: bool = False,
    skip_probes: bool = False,
) -> list[str]:
    """One pass over every system. Returns printable log lines."""
    probes = PROBES if probes is None else probes
    now = now or _now()
    state = _load_state()
    lines: list[str] = []
    for key, system in SYSTEMS.items():
        entry = state.get(key, {})
        login_time = authed_at(key, entry)
        if login_time and not entry.get("authed_at"):
            entry = {**entry, "authed_at": login_time.isoformat(timespec="seconds")}
        status = "unknown" if skip_probes else probes[key]()
        message, new_entry = evaluate(system, entry, status, now, login_time)
        age = f"{(now - login_time).total_seconds() / 86400:.1f}d" if login_time else "?"
        lines.append(
            f"{system.label:<13} probe={status:<8} last_login_age={age:<6} "
            f"lifetime={new_entry['lifetime_days']:g}d" + ("  -> message" if message else "")
        )
        if status_only:
            continue
        if message:
            lines.append(f"  {'would send' if dry_run else 'sending'}: {message.splitlines()[0]}")
            if dry_run or not send(message):
                if not dry_run:
                    new_entry.pop("last_alert_at", None)  # retry next run if delivery failed
        if not dry_run:
            state[key] = new_entry
    if not (dry_run or status_only):
        _save_state(state)
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true", help="print the table only; send and save nothing")
    ap.add_argument("--dry-run", action="store_true", help="show messages without sending or saving")
    ap.add_argument("--skip-probes", action="store_true", help="age-based reminders only (no live checks)")
    args = ap.parse_args()
    for line in run(dry_run=args.dry_run, status_only=args.status, skip_probes=args.skip_probes):
        print(line, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
