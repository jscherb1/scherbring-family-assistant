#!/usr/bin/env python3
"""Wellness CLI for the wellness subagent.

A thin wrapper over scripts/wellness.py (the shared service the dashboard's Wellness pages use too), so
the agent and the web UI run the exact same code. Read-only. Zero-dependency (Python stdlib only).

Usage:
    python scripts/wellness_store.py today
        (readiness, last night's sleep, vitals vs baseline, day rating, habits, data freshness)
    python scripts/wellness_store.py metric --key sleep_hours [--days 7]
        (one metric's daily points with status, rolling average and this-vs-previous-window summary)
    python scripts/wellness_store.py trends [--range 30|90|180]
    python scripts/wellness_store.py drivers [--target rating|hrv|sleep_score|resting_hr] [--days 90]
    python scripts/wellness_store.py scatter --x sleep_hours --y rating [--days 90]
    python scripts/wellness_store.py metrics
        (the metric keys, labels, units and timing classes)

All commands print JSON to stdout. Errors are {"error": ...} on stderr with exit code 1
(not found) or 2 (bad input).
"""

from __future__ import annotations

import argparse
import json
import sys

import wellness

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("today")
    p = sub.add_parser("metric")
    p.add_argument("--key", required=True)
    p.add_argument("--days", type=int, default=14)
    p = sub.add_parser("trends")
    p.add_argument("--range", default="90")
    p = sub.add_parser("drivers")
    p.add_argument("--target", default="rating")
    p.add_argument("--days", type=int, default=90)
    p = sub.add_parser("scatter")
    p.add_argument("--x", required=True)
    p.add_argument("--y", required=True)
    p.add_argument("--days", type=int, default=90)
    sub.add_parser("metrics")
    return ap


def run(args) -> int:
    if args.cmd == "today":
        _print(wellness.today_view())
    elif args.cmd == "metric":
        if not 1 <= args.days <= 366:
            raise wellness.WellnessError("--days must be 1-366")
        _print(wellness.series(args.key, args.days))
    elif args.cmd == "trends":
        _print(wellness.trends(args.range))
    elif args.cmd == "drivers":
        _print(wellness.drivers(args.target, days=args.days))
    elif args.cmd == "scatter":
        _print(wellness.scatter(args.x, args.y, days=args.days))
    elif args.cmd == "metrics":
        _print(list(wellness.METRICS.values()))
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except wellness.WellnessError as exc:
        print(json.dumps({"error": exc.message}), file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
