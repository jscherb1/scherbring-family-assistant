#!/usr/bin/env python3
"""Tell you on Telegram when a manually refreshed login has really stopped working.

Some integrations cannot re-authenticate themselves: Google Drive (consent),
Monarch (cookie paste). Hy-Vee is not here: scripts/hyvee/hyvee_connection.py owns it. This runs once a day from
systemd/assistant-credential-check.timer, costs no model tokens, and sends
direct Telegram messages so it still works if Claude itself is the broken piece.

Only a live probe (a real read against the service) can send a message:
  dead    -> one red "EXPIRED" message per day with the desktop shortcut to run
  ok      -> silence, however old the login is
  unknown -> silence (network trouble is not an expired login)
The age timer is a secondary signal: it is shown in --status, a dead probe
shortens that system's lifetime estimate to what was observed, and a login past
its estimate gets a quiet note appended to an EXPIRED message. It never sends
anything on its own. COROS is not listed: it re-logs in by itself.

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
ALERT_EVERY = timedelta(hours=20)  # at most one message per system per day


@dataclass(frozen=True)
class System:
    key: str
    label: str
    lifetime_days: float  # estimate until observed
    relogin: str  # what the user should do
    shortcut: str  # name of the desktop shortcut that refreshes it


SYSTEMS: dict[str, System] = {
    "drive": System(
        "drive", "Google Drive", 7,
        f"cd {REPO_ROOT} && .venv/bin/python scripts/drive_upload.py auth\n"
        "(open the printed URL in your browser and approve)",
        "Google Drive Login",
    ),
    "monarch": System(
        "monarch", "Monarch", 14,
        f"cd {REPO_ROOT}/vendor/monarch-mcp-server && .venv/bin/python login_setup.py\n"
        "(choose option 1 and paste the cookie header from app.monarch.com)",
        "Monarch Login",
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

        # A real, read-only Drive call: also catches a revoked token or the weekly Testing-mode expiry.
        drive_upload._service(interactive=False).files().list(pageSize=1, fields="files(id)").execute()
        return "ok"
    except RuntimeError:
        return "dead"  # missing token or a rejected refresh token
    except Exception as exc:  # noqa: BLE001 - network etc.
        status = getattr(getattr(exc, "resp", None), "status", None)
        return "dead" if status == 401 else "unknown"  # 403 can be a quota limit


PROBES: dict[str, Callable[[], str]] = {"drive": probe_drive, "monarch": probe_monarch}


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
        if age_days is not None and 1 <= age_days < lifetime:  # a brand-new login dying is a bad probe, not a lifetime
            # It died sooner than we assumed: learn from it.
            lifetime = max(1.0, round(age_days, 1))
            entry["lifetime_days"] = lifetime
        entry["status"] = "dead"
        if not due_for_message:
            return None, entry
        entry["last_alert_at"] = now.isoformat(timespec="seconds")
        since = f" (last login {_fmt_days(age_days)} days ago)" if age_days is not None else ""
        note = ""
        if age_days is not None and age_days >= lifetime:
            note = f" That is past the usual {_fmt_days(lifetime)} days."
        return (
            f"\U0001f534 {system.label} login has EXPIRED{since}. A live check confirmed it.{note} "
            f"Anything that needs it will fail until you re-login.\n\n"
            f"Easiest: run \"{system.shortcut}\" on your desktop.\n"
            f"Or from a terminal:\n{system.relogin}",
            entry,
        )

    # ok or unknown: the age timer alone never sends a message.
    entry["status"] = probe_status
    if probe_status == "ok":
        entry.pop("last_alert_at", None)  # recovered: re-arm the alert
    return None, entry


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
    ap.add_argument("--skip-probes", action="store_true", help="skip the live checks (nothing can be sent)")
    args = ap.parse_args()
    for line in run(dry_run=args.dry_run, status_only=args.status, skip_probes=args.skip_probes):
        print(line, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
