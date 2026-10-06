#!/usr/bin/env python3
"""Detect a stalled scheduler dispatcher, recover it, and alert over Telegram.

scheduler_dispatch.py stamps
state/scheduler_loop_state.json `last_tick` on every run (every 2 minutes via
systemd/assistant-scheduler.timer). A stale or missing tick means the timer
stopped firing; a stale tick is not evidence that the orchestrator is broken.

An unhealthy reading must persist for 5 minutes (state/scheduler_watchdog.json)
before acting. Recovery:
  1. run scheduler_dispatch.py once directly to restore the heartbeat;
  2. (re)start assistant-scheduler.timer;
  3. alert via the Telegram Bot API (no Claude/MCP involved) if the timer was
     not running, or fired recently yet the tick is still stale (a real bug).
     If it is running but did not fire either, the machine was most likely
     asleep, so recover silently.

Malformed state is treated as unknown, never as unhealthy.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from paths import REPO_ROOT

STALE_MINUTES = 20
DEBOUNCE_MINUTES = 5
TIMER_UNIT = "assistant-scheduler.timer"


def _parse_local(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.astimezone()


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def assess(loop_state_file: Path, now: datetime) -> tuple[bool, str | None]:
    """Return (unhealthy, reason). Corrupt files are unknown, so healthy."""
    if not loop_state_file.exists():
        return True, "scheduler heartbeat file missing (state/scheduler_loop_state.json not found)"
    data = _load_json(loop_state_file)
    try:
        age = (now - _parse_local(data["last_tick"])).total_seconds() / 60  # type: ignore[index]
    except (TypeError, KeyError, ValueError):
        return False, None
    if age >= STALE_MINUTES:
        return True, f"scheduler poll loop stale (last tick {age:.1f} min ago)"
    return False, None


def timer_state() -> tuple[bool, datetime | None]:
    """(timer is active, when it last fired). Unknown values are (False, None)."""
    result = subprocess.run(
        ["systemctl", "--user", "show", "-p", "ActiveState", "-p", "LastTriggerUSec", TIMER_UNIT],
        capture_output=True, text=True,
    )
    props = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    active = props.get("ActiveState") == "active"
    # LastTriggerUSec prints like "Mon 2026-10-05 14:51:34 CDT"; the date and
    # time are local, so the timezone abbreviation is dropped.
    parts = props.get("LastTriggerUSec", "").split()
    if len(parts) < 3:
        return active, None
    try:
        naive = datetime.strptime(f"{parts[1]} {parts[2].split('.')[0]}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return active, None
    return active, naive.astimezone()


def run_dispatch_directly(repo_root: Path) -> None:
    subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "scheduler_dispatch.py")],
        capture_output=True, text=True, cwd=str(repo_root),
    )


def restart_timer() -> None:
    subprocess.run(["systemctl", "--user", "restart", TIMER_UNIT], capture_output=True, text=True)


def send_alert(text: str) -> None:
    try:
        import telegram_send as ts

        token, chat_id = ts._read_token(), ts._default_chat_id()
        if token and chat_id:
            ts._send_raw(token, chat_id, text)
    except Exception as exc:  # noqa: BLE001
        print(f"watchdog: failed to send direct Telegram alert: {exc}")


def run(
    repo_root: Path = REPO_ROOT,
    now: datetime | None = None,
    dispatch: Callable[[Path], None] = run_dispatch_directly,
    restart: Callable[[], None] = restart_timer,
    timer: Callable[[], tuple[bool, datetime | None]] = timer_state,
    alert: Callable[[str], None] = send_alert,
) -> list[str]:
    """One watchdog pass. Returns log lines."""
    now = (now or datetime.now(timezone.utc)).astimezone()
    state_file = repo_root / "state" / "scheduler_watchdog.json"
    lines: list[str] = []

    unhealthy, reason = assess(repo_root / "state" / "scheduler_loop_state.json", now)
    state = _load_json(state_file) or {}
    state.setdefault("first_unhealthy_at", None)
    state.setdefault("last_restart_at", None)

    if not unhealthy:
        if state["first_unhealthy_at"]:
            state["first_unhealthy_at"] = None
            _save_state(state_file, state)
            lines.append("watchdog: scheduler poll loop recovered on its own - clearing tracking")
        return lines

    if not state["first_unhealthy_at"]:
        state["first_unhealthy_at"] = now.isoformat()
        _save_state(state_file, state)
        lines.append(f"watchdog: {reason} - starting {DEBOUNCE_MINUTES}-minute confirmation window")
        return lines

    if (now - _parse_local(state["first_unhealthy_at"])).total_seconds() / 60 < DEBOUNCE_MINUTES:
        return lines

    lines.append(f"watchdog: persistent scheduler stall confirmed ({reason}) - recovering")
    dispatch(repo_root)
    lines.append("watchdog: ran scheduler_dispatch.py directly")
    restart()
    lines.append(f"watchdog: restarted {TIMER_UNIT}")

    state["first_unhealthy_at"] = None
    state["last_restart_at"] = now.isoformat()
    _save_state(state_file, state)

    timer_active, last_fire = timer()
    if not timer_active:
        alert(f"\u26a0\ufe0f Scheduler dispatch stalled ({reason}). {TIMER_UNIT} was not running and has been restarted.")
    elif last_fire is not None and (now - last_fire).total_seconds() / 60 < STALE_MINUTES:
        alert(f"\u26a0\ufe0f Scheduler dispatch stalled ({reason}). The scheduler timer was restarted to recover.")
    else:
        lines.append("watchdog: stale tick correlates with a stale timer (machine was likely asleep) - recovered silently")
    return lines


def main() -> int:
    for line in run():
        print(line, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
