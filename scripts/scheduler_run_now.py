#!/usr/bin/env python3
"""Run one scheduled task right now through the real dispatcher code path.

For testing. It calls scheduler_dispatch.dispatch_task, so the claude command,
Sonnet pin, MCP timeouts, marker extraction and Telegram delivery are exactly
what a scheduled run uses. Unlike a scheduled run it does NOT call
mark-dispatched, so the task's normal schedule is unaffected. The run is logged
in scheduled_task_runs with a "[manual test]" summary prefix, and a failure
sends the usual failure alert.

Because this is a test, the prompt gets an extra paragraph asking for a
"[TEST]" message prefix and for a message even when the task would normally
stay silent, so you can see that delivery works.

Usage:
    python scripts/scheduler_run_now.py --name weekly-meal-plan
    python scripts/scheduler_run_now.py --name monthly-spending-summary --ignore-gates
    python scripts/scheduler_run_now.py --list
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Reproduce the systemd unit's PATH (see systemd/assistant-scheduler.service) BEFORE
# importing the dispatcher: the project venv first, so the bare `python scripts/...`
# commands the agents run resolve to it. Ubuntu has no `python`, so without this a
# manual run from a normal shell silently breaks every agent script call.
_REPO = Path(__file__).resolve().parent.parent
os.environ["PATH"] = os.pathsep.join(
    [str(_REPO / ".venv" / "bin"), str(Path.home() / ".local" / "bin"), str(Path.home() / ".bun" / "bin"),
     os.environ.get("PATH", "")]
)

import scheduler_dispatch as sd  # noqa: E402

TEST_NOTE = (
    "\n\n(MANUAL TEST RUN requested by the owner to verify this task works. Begin any "
    "Telegram message with '[TEST] '. If the workflow would normally stay silent / send "
    "nothing, instead send one short '[TEST]' message saying what you checked and that "
    "nothing needed reporting. If anything fails or a tool is unavailable, say exactly "
    "which one in the message.)"
)
GATE_NOTE = (
    "\n\n(For this test, IGNORE any date-based self-gate (last Saturday, first Sunday, "
    "month, quarter, etc.) in this task and in the subagent's own workflow: run the full "
    "workflow as if today qualified.)"
)


def _store(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(sd.SCRIPTS_DIR / "scheduler_store.py"), *args],
        capture_output=True, text=True, encoding="utf-8", cwd=str(sd.REPO_ROOT),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", help="scheduled task name")
    ap.add_argument("--ignore-gates", action="store_true", help="override date self-gates so the full workflow runs")
    ap.add_argument("--list", action="store_true", help="list task names and exit")
    args = ap.parse_args()

    if args.list:
        for t in json.loads(_store("list").stdout):
            print(f"{t['name']:<30} {t['cron_expression']}")
        return 0
    if not args.name:
        ap.error("--name is required (or use --list)")

    got = _store("get", "--name", args.name)
    if got.returncode != 0:
        print(f"no scheduled task named {args.name!r}", file=sys.stderr)
        return 2
    task = json.loads(got.stdout)
    run_at = datetime.now().strftime("%Y-%m-%dT%H:%M:00")
    task["due_run_at"] = run_at
    task["prompt"] = task["prompt"] + TEST_NOTE + (GATE_NOTE if args.ignore_gates else "")

    started = time.time()
    success, summary = sd.dispatch_task(task)
    seconds = time.time() - started

    sd.log_run(task["id"], run_at, "ok" if success else "failed", f"[manual test] {summary}")
    if not success:
        sd.send_failure_alert(task["name"], run_at, summary)
    print(json.dumps({
        "task": task["name"],
        "status": "ok" if success else "failed",
        "seconds": round(seconds),
        "ignore_gates": args.ignore_gates,
        "summary": summary[:600],
        "debug_log": str(sd.DISPATCH_DEBUG_DIR / f"{task['id']}_{run_at.replace(':', '-')}.log"),
    }, indent=2))
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
