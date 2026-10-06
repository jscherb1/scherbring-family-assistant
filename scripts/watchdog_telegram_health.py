#!/usr/bin/env python3
"""Detect a stuck Telegram MCP binding in the running orchestrator and kill claude.

The signals below each come from a past incident (see the notes in memory and git
history). start_orchestrator.sh's loop restarts
claude ~10 s after it dies, and telegram_context_bridge.py replays the last
60 minutes of conversation into the fresh session.

Unhealthy signals (any one is enough):
  1. log grep: the reply tool "not found in render-time tools" appears more
     recently than the last healthy connection/reply line in
     state/logs/orchestrator_debug.log.
  2. fallback self-report: state/telegram_fallback_used.json (written by
     telegram_send.py) is newer than the last watchdog restart.
  3. stale-silent: no verified reply/connection for 60+ minutes AND an SSE
     reconnect/liveness timeout since the last verification.

An unhealthy reading must persist for 3 minutes (state/telegram_watchdog.json)
before the kill. No log, no orchestrator process, or no signal means no action:
absence of data never triggers a restart.

Run every 2 minutes by systemd/assistant-watchdog.timer.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from paths import REPO_ROOT

PROCESS_PATTERN = "channels.*plugin:telegram"
TAIL_LINES = 3000
CONFIRM_MINUTES = 3
STALE_SILENT_MINUTES = 60

UNHEALTHY_RE = re.compile(
    r"Tool mcp__plugin_telegram_telegram__reply not found in render-time tools"
    r"|Filtering out tool_reference for unavailable tool: mcp__plugin_telegram_telegram__reply"
)
HEALTHY_RE = re.compile(
    r'MCP server "plugin:telegram:telegram": Successfully connected'
    r'|MCP server "plugin:telegram:telegram": Tool .reply. completed successfully'
)
VERIFIED_RE = re.compile(
    r"Tool 'reply' completed successfully"
    r'|plugin:telegram:telegram": Successfully connected'
)
RECONNECT_RE = re.compile(r"SSETransport: Stream read error|SSETransport: Liveness timeout, reconnecting")
LOG_TS_RE = re.compile(r"^(\S+Z)")


def _log_time(line: str) -> datetime | None:
    m = LOG_TS_RE.match(line)
    if not m:
        return None
    try:
        return datetime.fromisoformat(m.group(1))
    except ValueError:
        return None


def _parse_local(value: str) -> datetime:
    """Parse an ISO timestamp; naive values are local time."""
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.astimezone()


def _last_index(lines: list[str], pattern: re.Pattern[str]) -> int | None:
    for i in range(len(lines) - 1, -1, -1):
        if pattern.search(lines[i]):
            return i
    return None


def evaluate(
    tail: list[str],
    state: dict,
    fallback: dict | None,
    now: datetime,
) -> tuple[bool, str]:
    """Return (unhealthy, source description). `now` must be timezone-aware."""
    last_bad = _last_index(tail, UNHEALTHY_RE)
    last_good = _last_index(tail, HEALTHY_RE)
    if last_bad is not None and (last_good is None or last_bad > last_good):
        return True, "log grep"

    if fallback and fallback.get("last_used_at"):
        try:
            used_at = _parse_local(fallback["last_used_at"])
            restarted = state.get("last_restart_at")
            if not restarted or used_at > _parse_local(restarted):
                return True, f"fallback-script signal ({fallback.get('reason')})"
        except ValueError:
            pass

    verified_idx = _last_index(tail, VERIFIED_RE)
    if verified_idx is not None:
        verified_at = _log_time(tail[verified_idx])
        if verified_at:
            minutes = (now - verified_at).total_seconds() / 60
            if minutes >= STALE_SILENT_MINUTES:
                for line in tail[verified_idx + 1:]:
                    if RECONNECT_RE.search(line):
                        ts = _log_time(line)
                        if ts and ts > verified_at:
                            return True, f"no verified reply for {minutes:.1f} min with a reconnect since"
    return False, ""


def find_orchestrator_pid() -> int | None:
    result = subprocess.run(["pgrep", "-f", PROCESS_PATTERN], capture_output=True, text=True)
    own = {os.getpid(), os.getppid()}
    for token in result.stdout.split():
        pid = int(token)
        if pid not in own:
            return pid
    return None


def kill_pid(pid: int) -> None:
    os.kill(pid, signal.SIGKILL)


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def run(
    repo_root: Path = REPO_ROOT,
    now: datetime | None = None,
    find_pid: Callable[[], int | None] = find_orchestrator_pid,
    kill: Callable[[int], None] = kill_pid,
) -> str | None:
    """One watchdog pass. Returns a log line when something noteworthy happened."""
    now = now or datetime.now(timezone.utc)
    state_file = repo_root / "state" / "telegram_watchdog.json"
    log_file = repo_root / "state" / "logs" / "orchestrator_debug.log"
    fallback_file = repo_root / "state" / "telegram_fallback_used.json"

    pid = find_pid()
    if pid is None or not log_file.exists():
        return None
    with log_file.open(encoding="utf-8", errors="replace") as fh:
        tail = [line.rstrip("\n") for line in deque(fh, maxlen=TAIL_LINES)]
    if not tail:
        return None

    state = _load_json(state_file) or {}
    state.setdefault("first_unhealthy_at", None)
    state.setdefault("last_restart_at", None)

    unhealthy, source = evaluate(tail, state, _load_json(fallback_file), now)
    if unhealthy:
        if not state["first_unhealthy_at"]:
            state["first_unhealthy_at"] = now.astimezone().isoformat()
            _save_state(state_file, state)
            return f"watchdog: unhealthy signal seen via {source}, starting {CONFIRM_MINUTES}-minute confirmation window"
        first_seen = _parse_local(state["first_unhealthy_at"])
        if (now - first_seen).total_seconds() / 60 >= CONFIRM_MINUTES:
            kill(pid)
            state["first_unhealthy_at"] = None
            state["last_restart_at"] = now.astimezone().isoformat()
            _save_state(state_file, state)
            return f"watchdog: persistent broken Telegram connection confirmed - killed orchestrator (pid {pid})"
        return None
    if state["first_unhealthy_at"]:
        state["first_unhealthy_at"] = None
        _save_state(state_file, state)
        return "watchdog: Telegram connection recovered on its own - clearing tracking"
    return None


def main() -> int:
    message = run()
    if message:
        print(message, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
