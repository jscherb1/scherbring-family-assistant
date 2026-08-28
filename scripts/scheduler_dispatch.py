#!/usr/bin/env python3
"""External scheduler dispatcher — runs every 2 minutes via Windows Task Scheduler.

Writes a heartbeat tick to state/scheduler_loop_state.json, then queries for due
scheduled tasks. If nothing is due, exits immediately with zero Claude API calls.
When tasks are due, dispatches each via a one-shot `claude --print` subprocess.

This replaces the in-session CronCreate poll loop that previously ran inside the
orchestrator session, which was burning ~720 idle Claude turns per day (one every
2 minutes even when nothing was scheduled). The poll logic now lives entirely
outside Claude; tokens are consumed only when a task actually fires.

Deliberately does NOT pass --channels plugin:telegram to the dispatched subprocess.
Attaching the real Telegram tool tempts the one-shot subagent into calling it
directly instead of following the marker-wrapping instruction below, and that
in-process tool call is unreliable for a fresh one-shot session (silently fails,
or the subagent hallucinates success) — see memory: scheduler_dispatch_false_ok_gap.
Without the tool available, the subagent reliably falls back to the markers,
which we then deliver ourselves via a direct Bot API call.

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
DISPATCH_DEBUG_DIR = REPO_ROOT / "state" / "logs" / "scheduler_dispatch_debug"
TASK_TIMEOUT_SECONDS = 300  # 5 minutes per task

# The local `monarch` MCP server is a stdio server spawned fresh per process
# (Python venv cold start + heavy monarchmoney/gql imports + a live Windows
# keyring round-trip probe). Observed cold-start connect times in
# state/logs/orchestrator_debug.log range from ~3s to ~24.5s, with at least one
# documented outright failure at Claude Code's default ~30000ms MCP connect
# timeout ("Connection timeout triggered after 30022ms (limit: 30000ms)"). A
# one-shot `claude --print` dispatch process races against that same default
# timeout every run, and losing the race silently drops the monarch tools
# before the finance subagent is delegated to — see memory:
# finance_subagent_missing_monarch_tools_gap. Give it real headroom.
MCP_TIMEOUT_MS = "60000"


def _resolve_claude() -> str:
    """Find the claude executable, preferring the real .exe over the npm .cmd shim.

    npm's generated claude.cmd on Windows forwards args via a bare `%*`, which
    mangles/truncates multi-line prompt arguments when invoked through
    subprocess.run's list-argv form (confirmed by direct testing: the same
    multi-line prompt reliably lost its marker instructions through claude.cmd
    but worked every time through the underlying claude.exe). This is the root
    cause of scheduled tasks' Telegram replies never arriving despite "ok"
    status — see memory: scheduler_dispatch_false_ok_gap.
    """
    npm_dir = Path(os.environ.get("APPDATA", "")) / "npm"
    exe_candidate = npm_dir / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    if exe_candidate.exists():
        return str(exe_candidate)
    # shutil.which respects PATHEXT so it finds .cmd/.exe on Windows.
    found = shutil.which("claude")
    if found:
        return found
    # Fallback: npm global install location for the current user.
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


def _telegram_send_raw(token: str, chat_id: str, text: str) -> None:
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10)  # noqa: S310


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
        _telegram_send_raw(token, chat_id, text)
    except Exception as exc:  # noqa: BLE001
        print(f"scheduler_dispatch: failed to send failure alert: {exc}", flush=True)


# Markers the dispatched subagent is instructed to wrap its intended Telegram
# message in. Delivery happens here, via direct Bot API call, rather than
# trusting the subagent's own in-process Telegram MCP tool call — a one-shot
# headless `claude --print` run gets fresh MCP connections every time, and if
# the Telegram tool isn't connected yet when the subagent calls it, that call
# can silently fail (or never happen) while the process still exits 0. See
# memory: scheduler_dispatch_false_ok_gap.
TELEGRAM_SEND_START = "<<<TELEGRAM_SEND>>>"
TELEGRAM_SEND_END = "<<<TELEGRAM_SEND_END>>>"
TELEGRAM_MAX_CHARS = 4000


def extract_telegram_message(output: str):
    """Pull the marked message out of the subagent's stdout, if present."""
    start = output.find(TELEGRAM_SEND_START)
    end = output.find(TELEGRAM_SEND_END)
    if start == -1 or end == -1 or end < start:
        return None
    return output[start + len(TELEGRAM_SEND_START):end].strip()


def deliver_telegram_message(chat_id: str, text: str) -> bool:
    """Send `text` to `chat_id` directly via the Bot API, chunked if long.

    Returns True only if every chunk was accepted by the API.
    """
    token, _ = _read_telegram_creds()
    if not token or not text:
        return False
    chunks = [text[i:i + TELEGRAM_MAX_CHARS] for i in range(0, len(text), TELEGRAM_MAX_CHARS)] or [text]
    try:
        for chunk in chunks:
            _telegram_send_raw(token, chat_id, chunk)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"scheduler_dispatch: failed to deliver telegram message: {exc}", flush=True)
        return False


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
        encoding="utf-8",
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
        encoding="utf-8",
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
    subprocess.run(args, capture_output=True, text=True, encoding="utf-8", cwd=str(REPO_ROOT))


def dispatch_task(task: dict) -> tuple[bool, str]:
    """Run one due task via claude --print. Returns (success, summary)."""
    task_id = str(task.get("id", ""))
    name = task.get("name", task_id)
    prompt = task.get("prompt", "")
    chat_id = str(task.get("target_chat_id", "") or "")
    run_at = str(task.get("due_run_at", "") or "")

    if not prompt:
        return False, "no prompt defined on task"

    print(f"scheduler_dispatch: dispatching '{name}' (id={task_id})", flush=True)

    # This is a one-shot headless run with no inbound Telegram message. Fresh MCP
    # connections spin up every time, so the subagent's own Telegram reply tool
    # call can silently fail (or never fire) if that tool isn't connected yet —
    # exit code 0 either way. So instead of trusting the subagent to deliver its
    # own message, we have it hand the text back to us (wrapped in markers) and
    # deliver it ourselves via direct Bot API call — the same mechanism already
    # proven reliable for send_failure_alert.
    full_prompt = (
        f"{prompt}\n\n"
        "(You are running as a one-shot scheduled task dispatch, NOT as a live "
        "Telegram channel event - there is no <channel> tag or chat_id in this "
        "invocation, and that is expected; do not ask for one or refuse on that "
        "basis. You have no Telegram tool available. Whenever the workflow above "
        "asks you to reply/post/send a message to the Telegram chat, output that "
        f"exact message text wrapped between the literal markers {TELEGRAM_SEND_START} "
        f"and {TELEGRAM_SEND_END} as the very last part of your final response, "
        "with nothing else between the markers - an external dispatcher delivers "
        "it for you. If the workflow says to reply with nothing / stay silent, do "
        "not include those markers at all.)"
        if chat_id
        else prompt
    )

    try:
        # Do NOT pass --channels here. Attaching the real Telegram tool tempts the
        # one-shot subagent into calling it directly instead of following the
        # marker-wrapping instruction above — confirmed via direct testing: with
        # --channels present the subagent either refused (claiming no chat_id) or
        # called the tool and self-reported success while nothing was delivered,
        # non-deterministically, every time. Without --channels there is no tool
        # to reach for, and the subagent reliably emits the markers instead, which
        # we then deliver ourselves via direct Bot API call below.
        env = os.environ.copy()
        env["MCP_TIMEOUT_MS"] = MCP_TIMEOUT_MS
        env["MCP_CONNECT_TIMEOUT_MS"] = MCP_TIMEOUT_MS

        DISPATCH_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        safe_run_at = run_at.replace(":", "-") or "unknown"
        debug_log = DISPATCH_DEBUG_DIR / f"{task_id or 'unknown'}_{safe_run_at}.log"
        result = subprocess.run(
            [CLAUDE_EXE, "--print", "--debug-file", str(debug_log), full_prompt],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(REPO_ROOT),
            timeout=TASK_TIMEOUT_SECONDS,
            env=env,
        )
        output = (result.stdout or "").strip()
        process_ok = result.returncode == 0

        message = extract_telegram_message(output) if (process_ok and chat_id) else None
        if message:
            delivered = deliver_telegram_message(chat_id, message)
            success = process_ok and delivered
            summary = message[:500] if delivered else f"telegram delivery failed; message was: {message[:400]}"
        else:
            success = process_ok
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
