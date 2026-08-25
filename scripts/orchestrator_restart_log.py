#!/usr/bin/env python3
"""Records every orchestrator session launch so restart counts stay queryable.

Justin (the user) has no visibility into the orchestrator restarting - it just
happens and the conversation picks up in a new session. Since restarts are
now designed to feel seamless (see scripts/telegram_context_bridge.py), that
also means they'd otherwise be invisible. This gives an explicit, queryable
record instead of relying on him noticing.

start_orchestrator.ps1 calls `record` at the top of every loop iteration,
before each `claude` launch, tagging it "wrapper-start" (the first launch of
a given wrapper process, e.g. after logon or a full manual restart) or
"loop-restart" (claude.exe exited - crash, watchdog kill, etc - and the
wrapper's own while loop brought it back up without the outer process dying).
That single call site is the only place sessions are recorded, so a
watchdog-triggered restart is captured automatically as a loop-restart
without the watchdog scripts needing to log anything themselves.

Usage:
    python scripts/orchestrator_restart_log.py record --session-name NAME --reason wrapper-start|loop-restart
    python scripts/orchestrator_restart_log.py summary
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = REPO_ROOT / "state" / "orchestrator_restarts.jsonl"

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_entries() -> list[dict]:
    if not LOG_FILE.exists():
        return []
    entries = []
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def cmd_record(args: argparse.Namespace) -> int:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": _utc_now_iso(), "session_name": args.session_name, "reason": args.reason}
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(json.dumps(entry))
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    entries = _load_entries()
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff_24h = now - timedelta(hours=24)

    def _parse(e: dict) -> datetime | None:
        try:
            return datetime.strptime(e["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            return None

    today_count = 0
    last_24h_count = 0
    loop_restart_count = 0
    wrapper_start_count = 0
    for e in entries:
        dt = _parse(e)
        if dt is None:
            continue
        if dt >= today_start:
            today_count += 1
        if dt >= cutoff_24h:
            last_24h_count += 1
        if e.get("reason") == "loop-restart":
            loop_restart_count += 1
        elif e.get("reason") == "wrapper-start":
            wrapper_start_count += 1

    last = entries[-1] if entries else None
    summary = {
        "total_launches": len(entries),
        "today": today_count,
        "last_24h": last_24h_count,
        "wrapper_starts_total": wrapper_start_count,
        "loop_restarts_total": loop_restart_count,
        "last_launch_at": last.get("ts") if last else None,
        "last_launch_reason": last.get("reason") if last else None,
    }
    print(json.dumps(summary, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Orchestrator restart/launch log.")
    sub = parser.add_subparsers(dest="command", required=True)

    r = sub.add_parser("record", help="Append a launch record.")
    r.add_argument("--session-name", required=True)
    r.add_argument("--reason", required=True, choices=["wrapper-start", "loop-restart"])
    r.set_defaults(func=cmd_record)

    s = sub.add_parser("summary", help="Print restart counts.")
    s.set_defaults(func=cmd_summary)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
