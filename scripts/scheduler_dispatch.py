#!/usr/bin/env python3
"""External scheduler dispatcher — runs every 2 minutes via Windows Task Scheduler.

Writes a heartbeat tick to state/scheduler_loop_state.json, then queries for due
scheduled tasks. If nothing is due, exits immediately with zero Claude API calls.
When tasks are due, dispatches each via a one-shot `claude --print` subprocess.

This replaces the in-session CronCreate poll loop that previously ran inside the
orchestrator session, which was burning ~720 idle Claude turns per day (one every
2 minutes even when nothing was scheduled). The poll logic now lives entirely
outside Claude; tokens are consumed only when a task actually fires.

Correct claude --print invocation (--channels is variadic so the prompt must
come BEFORE --channels or it gets consumed as a channel name):
    claude --print "<prompt>" --channels plugin:telegram@claude-plugins-official

Usage (via Task Scheduler):
    python scripts/scheduler_dispatch.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOOP_STATE_FILE = REPO_ROOT / "state" / "scheduler_loop_state.json"
SCRIPTS_DIR = REPO_ROOT / "scripts"
CLAUDE_CHANNELS = "plugin:telegram@claude-plugins-official"
TASK_TIMEOUT_SECONDS = 300  # 5 minutes per task


def _resolve_claude() -> str:
    """Find the claude executable, preferring claude.cmd (npm install on Windows)."""
    # shutil.which respects PATHEXT so it finds .cmd/.exe on Windows.
    found = shutil.which("claude")
    if found:
        return found
    # Fallback: npm global install location for the current user.
    npm_dir = Path(os.environ.get("APPDATA", "")) / "npm"
    for name in ("claude.cmd", "claude.exe", "claude"):
        candidate = npm_dir / name
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError(
        "claude executable not found. Ensure @anthropic-ai/claude-code is installed "
        "globally via npm and the npm bin directory is in PATH."
    )


CLAUDE_EXE = _resolve_claude()


CONFIG_FILE = REPO_ROOT / "scripts" / "scheduler.config.json"
TELEGRAM_ENV_FILE = Path(os.environ.get("USERPROFILE", "")) / ".claude" / "channels" / "telegram" / ".env"


def _read_telegram_creds() -> tuple[str, str] | tuple[None, None]:
    """Return (bot_token, chat_id) from env file + config, or (None, None) if unavailable."""
    try:
        token = None
        if TELEGRAM_ENV_FILE.exists():
            for line in TELEGRAM_ENV_FILE.read_text(encoding="utf-8").splitlines():
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    token = line.split("=", 1)[1].strip()
        if not token:
            return None, None
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        chat_id = config.get("alert_chat_id")
        if not chat_id:
            return None, None
        return token, str(chat_id)
    except Exception:  # noqa: BLE001
        return None, None


def send_failure_alert(task_name: str, run_at: str, reason: str) -> None:
    """Send a direct Telegram alert when a task fails — no Claude involved."""
    token, chat_id = _read_telegram_creds()
    if not token:
        return
    text = (
        f"\u26a0\ufe0f Scheduled task failed: {task_name}\n"
        f"Due: {run_at}\n"
        f"Reason: {reason[:300]}"
    )
    try:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)  # noqa: S310
    except Exception as exc:  # noqa: BLE001
        print(f"scheduler_dispatch: failed to send failure alert: {exc}", flush=True)


def _now_local_iso() -> str:
    # Local wall-clock time, no timezone suffix. PowerShell's [datetime] cast treats
    # a bare ISO string as local time, which is correct for the watchdog comparison.
    # Do NOT add a Z suffix — Z causes PowerShell to convert UTC→local, making the
    # tick appear ~UTC-offset hours stale (e.g. 5 hours old on CDT = UTC-5).
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def update_heartbeat() -> None:
    """Update last_tick in scheduler_loop_state.json so the watchdog sees a fresh tick."""
    LOOP_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state: dict = {}
    if LOOP_STATE_FILE.exists():
        try:
            state = json.loads(LOOP_STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    state["last_tick"] = _now_local_iso()
    LOOP_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def get_due_tasks() -> list[dict]:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "scheduler_store.py"), "due"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    if result.returncode != 0:
        print(
            f"scheduler_dispatch: scheduler_store.py due failed (exit {result.returncode}): "
            f"{result.stderr.strip()}",
            flush=True,
        )
        return []
    try:
        tasks = json.loads(result.stdout)
        return tasks if isinstance(tasks, list) else []
    except json.JSONDecodeError:
        print(
            f"scheduler_dispatch: failed to parse due output: {result.stdout!r}",
            flush=True,
        )
        return []


def mark_dispatched(task_id: str, run_at: str) -> bool:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "scheduler_store.py"),
            "mark-dispatched",
            "--id", task_id,
            "--run-at", run_at,
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    return result.returncode == 0


def log_run(task_id: str, run_at: str, status: str, summary: str = "") -> None:
    args = [
        sys.executable,
        str(SCRIPTS_DIR / "scheduler_store.py"),
        "log-run",
        "--task-id", task_id,
        "--run-at", run_at,
        "--status", status,
    ]
    if summary:
        args += ["--summary", summary[:500]]
    subprocess.run(args, capture_output=True, text=True, cwd=str(REPO_ROOT))


def dispatch_task(task: dict) -> tuple[bool, str]:
    """Run one due task via claude --print. Returns (success, summary)."""
    task_id = str(task.get("id", ""))
    name = task.get("name", task_id)
    prompt = task.get("prompt", "")

    if not prompt:
        return False, "no prompt defined on task"

    print(f"scheduler_dispatch: dispatching '{name}' (id={task_id})", flush=True)

    try:
        # Prompt MUST come before --channels because --channels is variadic.
        result = subprocess.run(
            [CLAUDE_EXE, "--print", prompt, "--channels", CLAUDE_CHANNELS],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=TASK_TIMEOUT_SECONDS,
        )
        success = result.returncode == 0
        output = (result.stdout or "").strip()
        summary = output[:500] if output else (result.stderr or "").strip()[:200]
        status_label = "ok" if success else "failed"
        print(
            f"scheduler_dispatch: '{name}' {status_label} (exit {result.returncode})",
            flush=True,
        )
        return success, summary
    except subprocess.TimeoutExpired:
        print(
            f"scheduler_dispatch: '{name}' timed out after {TASK_TIMEOUT_SECONDS}s",
            flush=True,
        )
        return False, f"timed out after {TASK_TIMEOUT_SECONDS}s"
    except Exception as exc:  # noqa: BLE001
        print(f"scheduler_dispatch: '{name}' error: {exc}", flush=True)
        return False, str(exc)


def main() -> None:
    # Always update the heartbeat first — the watchdog reads this regardless of
    # whether any tasks are due.
    update_heartbeat()

    # Check for due tasks. Nothing due = exit immediately, zero tokens consumed.
    tasks = get_due_tasks()
    if not tasks:
        return

    print(f"scheduler_dispatch: {len(tasks)} task(s) due", flush=True)

    for task in tasks:
        task_id = str(task.get("id", ""))
        run_at = task.get("due_run_at", "")

        if not task_id or not run_at:
            print(
                f"scheduler_dispatch: skipping task with missing id/run_at: {task}",
                flush=True,
            )
            continue

        # Mark dispatched before executing to prevent duplicate dispatch if this
        # script is interrupted and re-run within the same 2-minute window.
        if not mark_dispatched(task_id, run_at):
            print(
                f"scheduler_dispatch: mark-dispatched failed for id={task_id}, skipping",
                flush=True,
            )
            continue

        success, summary = dispatch_task(task)
        log_run(task_id, run_at, "ok" if success else "failed", summary)
        if not success:
            send_failure_alert(task.get("name", task_id), run_at, summary)


if __name__ == "__main__":
    main()
