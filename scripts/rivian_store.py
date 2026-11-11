#!/usr/bin/env python3
"""Rivian (R2) status CLI for the ha-overview subagent. Read-only.

A thin wrapper over scripts/rivian.py (the shared service the dashboard's Rivian page uses too), so the agent
and the web UI run the exact same code. Zero-dependency (Python stdlib only).

Usage:
    python scripts/rivian_store.py brief                 (one-line answer: charge, range, charging, where)
    python scripts/rivian_store.py status [--section energy|charging|vehicle|closures|tires|climate|health]
    python scripts/rivian_store.py trends [--days 7|30|90]  (daily SOC, range, miles driven)
    python scripts/rivian_store.py trips [--days 7|30|90] [--no-sync]
        (trip-by-trip mi/kWh, totals, efficiency vs outside temp, parked drain; syncs the last 3 days first)
    python scripts/rivian_store.py sync [--days 1-10]   (re-derive trips/drain from HA history; the timer runs this)

Nothing here controls the car. All commands print JSON to stdout. Errors are {"error": ...} on stderr with
exit code 1 (not found / unreachable) or 2 (bad input).
"""

from __future__ import annotations

import argparse
import json
import sys

import rivian

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

SECTIONS = ("energy", "charging", "vehicle", "closures", "tires", "climate", "health")


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_brief(args) -> int:
    _print(rivian.brief())
    return 0


def cmd_status(args) -> int:
    result = rivian.status()
    _print(result[args.section] if args.section else result)
    return 0


def cmd_trends(args) -> int:
    _print(rivian.trends(args.days))
    return 0


def cmd_trips(args) -> int:
    if not args.no_sync:
        rivian.sync()
    _print(rivian.trips(args.days))
    return 0


def cmd_sync(args) -> int:
    _print(rivian.sync(args.days))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("brief").set_defaults(fn=cmd_brief)
    s = sub.add_parser("status"); s.add_argument("--section", choices=SECTIONS); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("trends"); s.add_argument("--days", type=int, default=30); s.set_defaults(fn=cmd_trends)
    s = sub.add_parser("trips"); s.add_argument("--days", type=int, default=30)
    s.add_argument("--no-sync", action="store_true"); s.set_defaults(fn=cmd_trips)
    s = sub.add_parser("sync"); s.add_argument("--days", type=int, default=rivian.SYNC_DAYS); s.set_defaults(fn=cmd_sync)
    try:
        args = p.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    try:
        return args.fn(args)
    except rivian.RivianError as exc:
        print(json.dumps({"error": exc.message}), file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
