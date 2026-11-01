#!/usr/bin/env python3
"""External scheduler dispatcher — runs every 2 minutes via assistant-scheduler.timer.

Writes a heartbeat tick to state/scheduler_loop_state.json, then queries for due
scheduled tasks. If nothing is due, exits immediately with zero Claude API calls.
When tasks are due, dispatches each via a one-shot `claude --print` subprocess.

This replaced an in-session CronCreate poll loop that previously ran inside the
agent session, which was burning ~720 idle Claude turns per day (one every
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
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import notify  # noqa: E402
from paths import REPO_ROOT, find_claude  # noqa: E402

LOOP_STATE_FILE = REPO_ROOT / "state" / "scheduler_loop_state.json"
SCRIPTS_DIR = REPO_ROOT / "scripts"
DISPATCH_DEBUG_DIR = REPO_ROOT / "state" / "logs" / "scheduler_dispatch_debug"
TASK_TIMEOUT_SECONDS = 300  # 5 minutes per task
# Project policy (CLAUDE.md): scheduled AI usage defaults to Sonnet. Pinned here so
# it does not depend on a per-machine ~/.claude/settings.json.
MODEL = "sonnet"

# The local `monarch` MCP server is a stdio server spawned fresh per process
# (Python venv cold start + heavy monarchmoney/gql imports + a live Windows
# keyring round-trip probe). Observed cold-start connect times in
# an earlier agent debug log ranged from ~3s to ~24.5s, with at least one
# documented outright failure at Claude Code's default ~30000ms MCP connect
# timeout ("Connection timeout triggered after 30022ms (limit: 30000ms)"). A
# one-shot `claude --print` dispatch process races against that same default
# timeout every run, and losing the race silently drops the monarch tools
# before the finance subagent is delegated to — see memory:
# finance_subagent_missing_monarch_tools_gap. Give it real headroom.
MCP_TIMEOUT_MS = "60000"


def _resolve_claude() -> str:
    """Find the claude executable via PATH, falling back to ~/.local/bin."""
    found = find_claude()
    if found:
        return found
    raise FileNotFoundError(
        "claude executable not found on PATH or in ~/.local/bin. Set PATH in the "
        "systemd unit (Environment=PATH=...) or install Claude Code."
    )


CLAUDE_EXE = _resolve_claude()


def send_failure_alert(task_name: str, run_at: str, reason: str) -> None:
    """Send a direct Telegram alert when a task fails — no Claude involved."""
    text = (
        f"\u26a0\ufe0f Scheduled task failed: {task_name}\n"
        f"Due: {run_at}\n"
        f"Reason: {reason[:300]}"
    )
    res = notify.send(text, source=f"scheduled:{task_name}", kind="alert")
    if not res.delivered and res.error:
        print(f"scheduler_dispatch: failed to send failure alert: {res.error}", flush=True)


# Markers the dispatched subagent is instructed to wrap its intended Telegram
# message in. Delivery happens here, via direct Bot API call, rather than
# trusting the subagent's own in-process Telegram MCP tool call — a one-shot
# headless `claude --print` run gets fresh MCP connections every time, and if
# the Telegram tool isn't connected yet when the subagent calls it, that call
# can silently fail (or never happen) while the process still exits 0. See
# memory: scheduler_dispatch_false_ok_gap.
TELEGRAM_SEND_START = "<<<TELEGRAM_SEND>>>"
TELEGRAM_SEND_END = "<<<TELEGRAM_SEND_END>>>"


def extract_telegram_message(output: str):
    """Pull the marked message out of the subagent's stdout, if present."""
    start = output.find(TELEGRAM_SEND_START)
    end = output.find(TELEGRAM_SEND_END)
    if start == -1 or end == -1 or end < start:
        return None
    return output[start + len(TELEGRAM_SEND_START):end].strip()


def deliver_telegram_message(chat_id: str, text: str, task_name: str = "") -> bool:
    """Send `text` to `chat_id` via notify (chunked, and recorded so the conversation knows about it,
    including any chunks that landed before a failure). True only if every chunk was accepted."""
    res = notify.send(text, source=f"scheduled:{task_name or 'task'}", kind="scheduled", chat_id=chat_id)
    if not res.delivered:
        print(f"scheduler_dispatch: failed to deliver telegram message: {res.error}", flush=True)
    return res.delivered


def count_permission_denials(debug_log: Path) -> int:
    """How many tool calls the headless run was denied.

    There is nobody to approve a prompt in a scheduled run, so every denial means the
    workflow could not do something it tried. `claude --print` still exits 0 and the
    agent usually still writes a message, so without this a degraded run looks "ok".
    """
    try:
        text = debug_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return text.count("tool permission denied")


def denied_tool_names(debug_log: Path) -> list[str]:
    """Tool names behind the denials, in order (the debug log doesn't record the command)."""
    try:
        text = debug_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return re.findall(r"(\S+) tool permission denied", text)


def _json_candidates(raw: str):
    """Yield stdout variants to try as JSON: whole text, last non-empty line, first `{` to last `}`."""
    yield raw
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    if lines:
        yield lines[-1]
    first, last = raw.find("{"), raw.rfind("}")
    if 0 <= first < last:
        yield raw[first:last + 1]


def _find_result(raw: str):
    """The result object from `claude --print --output-format json` stdout, or None."""
    for cand in _json_candidates(raw or ""):
        try:
            loaded = json.loads(cand)
        except (TypeError, ValueError):
            continue
        if isinstance(loaded, list):  # some versions emit an event array; the result is the last "result" item
            loaded = next((d for d in reversed(loaded) if isinstance(d, dict) and d.get("type") == "result"), None)
        if isinstance(loaded, dict) and ("result" in loaded or loaded.get("type") == "result"):
            return loaded
    return None


def extract_usage(raw: str) -> dict:
    """Cost fields from the claude JSON result: {"cost_usd", "duration_s", "model_usage"} (keys only when
    present and well-formed). Never raises: cost capture must not fail a run."""
    try:
        data = _find_result(raw)
        if not data:
            return {}
        out: dict = {}
        cost = data.get("total_cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            out["cost_usd"] = float(cost)
        ms = data.get("duration_ms")
        if isinstance(ms, (int, float)) and not isinstance(ms, bool):
            out["duration_s"] = round(ms / 1000.0, 1)
        mu = data.get("modelUsage")
        if isinstance(mu, dict) and mu:
            out["model_usage"] = json.dumps(mu, ensure_ascii=False, sort_keys=True)
        return out
    except Exception:  # noqa: BLE001
        return {}


def parse_claude_json(raw: str):
    """Parse `claude --print --output-format json` stdout.

    Returns (reply_text, denials, is_error), where denials is a list of {"tool_name", "tool_input"} dicts
    from the result's `permission_denials` and is_error is the result's own error flag. Returns None when no
    JSON result object can be found (a warning line before it is tolerated), so the caller falls back to
    treating stdout as plain text (the agent's reply must never be lost).
    """
    data = _find_result(raw)
    if data is None:
        return None
    raw_denials = data.get("permission_denials")
    denials = []
    if isinstance(raw_denials, list):
        for d in raw_denials:
            if isinstance(d, dict):
                denials.append({"tool_name": str(d.get("tool_name") or "unknown tool"), "tool_input": d.get("tool_input")})
    is_error = data.get("is_error") is True
    if isinstance(data.get("result"), str):
        text = data["result"].strip()
    else:  # error results (error_during_execution, error_max_turns, ...) carry no `result` string
        errs = data.get("errors")
        detail = f": {errs[0]}" if isinstance(errs, list) and errs else ""
        text = f"claude returned no result text (subtype {data.get('subtype') or 'unknown'}){detail}"[:300]
        is_error = True
    return text, denials, is_error


def describe_denial(denial: dict, width: int = 100) -> str:
    """One denied call as `Tool: command`, truncated. Bash shows its command; others show file_path if any."""
    name = denial.get("tool_name") or "unknown tool"
    inp = denial.get("tool_input")
    detail = ""
    if isinstance(inp, dict):
        detail = str(inp.get("command") or inp.get("file_path") or inp.get("path") or "")
    detail = " ".join(detail.split())
    if len(detail) > width:
        detail = detail[: width - 3] + "..."
    return f"{name}: {detail}" if detail else name


def summarize_denials(denials: list[dict], limit: int = 3) -> str:
    shown = "; ".join(describe_denial(d) for d in denials[:limit])
    more = len(denials) - limit
    return shown + (f"; +{more} more" if more > 0 else "")


# Phrases in the delivered reply that report the task's own outcome as a failure. Anchored to the task
# ("sync failed", "did not complete") so a report that merely mentions something else failing ("failed
# payment", "a check failed earlier and recovered") is not a false positive.
FAILURE_REPLY_RE = re.compile(
    r"(?:did\s+not|didn't|could\s+not|couldn't|unable\s+to|was\s+unable\s+to)\s+(?:complete|finish)|"
    r"failed\s+to\s+(?:deliver|complete|finish|run)|not\s+delivered|"
    r"\b(?:sync|review|refresh|run|task|job|upload|workflow|update)\s+(?:has\s+|had\s+)?"
    r"(?:failed|did\s+not\s+(?:run|work))",
    re.IGNORECASE,
)


def reply_says_failed(text: str) -> bool:
    return bool(FAILURE_REPLY_RE.search(text or ""))


def checkout_problem(repo: Path = REPO_ROOT) -> str:
    """Why the checkout isn't a clean `main`, or "" when it is (or git can't say).

    Scheduled runs read agents and scripts from the working tree, so a feature branch
    checked out for development silently changes what they can do (a missing command
    shows up as a vague permission denial). Naming it makes the real cause obvious.
    """
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=10
        ).stdout.strip()

    try:
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        if branch and branch != "main":
            return f"checkout is on branch '{branch}', not main"
        if branch and git("status", "--porcelain", "--untracked-files=no"):
            return "checkout has uncommitted changes to tracked files"
    except (OSError, subprocess.SubprocessError):
        return ""
    return ""


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


HEARTBEAT_EVERY_SECONDS = 60


class keep_heartbeat:
    """Refresh the heartbeat every minute while a long task runs, so the watchdog (which treats a tick older than
    20 minutes as a dead loop) doesn't fire during a task that has a long timeout_seconds."""

    def __enter__(self):
        self._stop = threading.Event()

        def beat():
            while not self._stop.wait(HEARTBEAT_EVERY_SECONDS):
                try:
                    update_heartbeat()
                except OSError:
                    pass

        self._thread = threading.Thread(target=beat, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        return False


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


def log_run(task_id: str, run_at: str, status: str, summary: str = "", usage: dict | None = None) -> None:
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
    usage = usage or {}
    if usage.get("cost_usd") is not None:
        args += ["--cost-usd", str(usage["cost_usd"])]
    if usage.get("duration_s") is not None:
        args += ["--duration-s", str(usage["duration_s"])]
    if usage.get("model_usage"):
        args += ["--model-usage-json", usage["model_usage"]]
    subprocess.run(args, capture_output=True, text=True, encoding="utf-8", cwd=str(REPO_ROOT))


def task_timeout(task: dict) -> int:
    """This task's run limit: its own timeout_seconds when set (NULL/0/garbage = the default)."""
    try:
        value = int(task.get("timeout_seconds") or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else TASK_TIMEOUT_SECONDS


def dispatch_task(task: dict, usage: dict | None = None) -> tuple[str, str]:
    """Run one due task via claude --print. Returns (status, summary).

    status is "ok", "degraded" (exited cleanly and delivered its message, but some commands were denied)
    or "failed".
    """
    task_id = str(task.get("id", ""))
    name = task.get("name", task_id)
    prompt = task.get("prompt", "")
    chat_id = str(task.get("target_chat_id", "") or "")
    run_at = str(task.get("due_run_at", "") or "")
    timeout = task_timeout(task)

    if not prompt:
        return "failed", "no prompt defined on task"

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
            [CLAUDE_EXE, "--print", "--output-format", "json", "--model", MODEL, "--debug-file", str(debug_log), full_prompt],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(REPO_ROOT),
            timeout=timeout,
            env=env,
        )
        raw_output = (result.stdout or "").strip()
        process_ok = result.returncode == 0

        # JSON output carries the exact denied commands. If it can't be parsed, fall back to the raw
        # stdout as the reply and to the debug-log denial count, so the reply text is never lost.
        parsed = parse_claude_json(raw_output)
        if usage is not None:
            usage.update(extract_usage(raw_output))
        reported_error = False
        if parsed is not None:
            output, denial_list, reported_error = parsed
            denial_count = len(denial_list)
            denial_text = summarize_denials(denial_list)
        else:
            output = raw_output
            denial_count = count_permission_denials(debug_log)
            denial_text = ", ".join(denied_tool_names(debug_log)) or "unknown tool"
        process_ok = process_ok and not reported_error

        message = extract_telegram_message(output) if (process_ok and chat_id) else None
        delivered = False
        if message:
            delivered = deliver_telegram_message(chat_id, message, name)
            success = process_ok and delivered
            summary = message[:500] if delivered else f"telegram delivery failed; message was: {message[:400]}"
        else:
            success = process_ok
            summary = output[:500] if output else (result.stderr or "").strip()[:200]

        status = "ok" if success else "failed"
        if denial_count:
            # degraded needs a delivered, marked message: a silent run whose commands were denied may have
            # concluded "nothing to report" from a blocked check, so it must stay failed and alert.
            if success and delivered and not reply_says_failed(message):
                status = "degraded"
            else:
                status = "failed"
            summary = f"[{denial_count} command(s) denied: {denial_text}] {summary}"
        problem = checkout_problem()
        if problem and status == "failed":
            summary = f"[possible cause: {problem}] {summary}"

        print(
            f"scheduler_dispatch: '{name}' {status} (exit {result.returncode})",
            flush=True,
        )
        return status, summary
    except subprocess.TimeoutExpired:
        print(
            f"scheduler_dispatch: '{name}' timed out after {timeout}s",
            flush=True,
        )
        return "failed", f"timed out after {timeout}s"
    except Exception as exc:  # noqa: BLE001
        print(f"scheduler_dispatch: '{name}' error: {exc}", flush=True)
        return "failed", str(exc)


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

        with keep_heartbeat():
            usage: dict = {}
            status, summary = dispatch_task(task, usage)
        log_run(task_id, run_at, status, summary, usage)
        if status == "failed":
            send_failure_alert(task.get("name", task_id), run_at, summary)


if __name__ == "__main__":
    main()
