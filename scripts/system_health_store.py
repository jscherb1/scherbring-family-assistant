#!/usr/bin/env python3
"""System health CLI for agents and the scheduler.

A thin wrapper over scripts/system_health.py (shared with the My Hub Systems tile).

Usage:
    python scripts/system_health_store.py status
    python scripts/system_health_store.py check [--system KEY ...]     (script-side checks; default all)

A system turning "down" (and later recovering) sends one Telegram message; staying down stays quiet.
    python scripts/system_health_store.py record KEY ok|warn|down|unknown [--detail TEXT]
        (for connector systems an agent just read from: todoist, calendar, gmail, home_assistant)

JSON on stdout. Errors are {"error": ...} on stderr with exit code 2 (bad input).
"""

from __future__ import annotations

import argparse
import json
import sys

import system_health as sh


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    c = sub.add_parser("check")
    c.add_argument("--system", action="append")
    r = sub.add_parser("record")
    r.add_argument("key")
    r.add_argument("status")
    r.add_argument("--detail", default="")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "status":
            out = sh.get_status()
        elif a.cmd == "check":
            out = sh.run_checks(a.system, notify=sh.telegram_notify)
        else:
            out = sh.record(a.key, a.status, a.detail, notify=sh.telegram_notify)
    except sh.HealthError as e:
        print(json.dumps({"error": e.message}), file=sys.stderr)
        return e.code
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
