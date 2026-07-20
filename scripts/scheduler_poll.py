#!/usr/bin/env python3
"""Poller driver for the scheduled-tasks feature.

Run every ~2 minutes by the "PersonalAssistantSchedulerPoller" Windows Scheduled Task
(scripts/register_scheduler_poller_task.ps1 / scripts/run_scheduler_poll.ps1). Zero
third-party dependencies (stdlib only, matching the rest of scripts/).

For each enabled scheduled task with a due, undispatched cron occurrence:
1. POST its prompt to the local scheduler channel server (scripts/scheduler_channel/).
2. On success (HTTP 200): advance last_dispatched_at and log a 'dispatched' run row.
3. On failure (channel not listening, orchestrator down, etc.): log a
   'dispatch_failed' run row and leave last_dispatched_at untouched, so the same
   occurrence is retried on the next poll rather than silently skipped.

Exits 0 even if individual dispatches fail - failures are recorded in
scheduled_task_runs for the heartbeat task to surface, not raised as a process error
(a single bad task should never take down the whole poll pass).
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_SCRIPT = REPO_ROOT / "scripts" / "scheduler_store.py"
CONFIG_PATH = REPO_ROOT / "scripts" / "scheduler.config.json"

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def _run_store(*args: str) -> dict | list:
    result = subprocess.run(
        [sys.executable, str(STORE_SCRIPT), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"scheduler_store.py {' '.join(args)} failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return json.loads(result.stdout)


def _post_event(host: str, port: int, task_id: str, task_name: str, chat_id: str, prompt: str) -> None:
    payload = json.dumps(
        {"task_id": task_id, "task_name": task_name, "chat_id": chat_id, "prompt": prompt}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"http://{host}:{port}/",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status != 200:
            raise RuntimeError(f"channel server returned HTTP {resp.status}")


def main() -> int:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    host, port = config["host"], config["port"]

    due = _run_store("due")
    if not due:
        print("no due tasks")
        return 0

    exit_code = 0
    for task in due:
        name = task["name"]
        task_id = task["id"]
        run_at = task["due_run_at"]
        try:
            _post_event(host, port, task_id, name, task["target_chat_id"], task["prompt"])
        except (urllib.error.URLError, RuntimeError) as exc:
            print(f"dispatch FAILED for {name!r}: {exc}", file=sys.stderr)
            _run_store(
                "log-run", "--task-id", task_id, "--run-at", run_at,
                "--status", "dispatch_failed", "--summary", str(exc)[:500],
            )
            exit_code = 1
            continue

        _run_store("mark-dispatched", "--id", task_id, "--run-at", run_at)
        _run_store(
            "log-run", "--task-id", task_id, "--run-at", run_at,
            "--dispatched-at", datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "--status", "dispatched",
        )
        print(f"dispatched {name!r} for {run_at}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
