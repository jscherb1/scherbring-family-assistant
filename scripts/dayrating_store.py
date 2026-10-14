#!/usr/bin/env python3
"""Day rating + insights CLI for the dayrating subagent.

A thin wrapper over scripts/dayrating.py and scripts/insights.py (the shared services the dashboard
uses too), so the agent and the web UI run the exact same code. Zero-dependency (Python stdlib only).

Usage:
    python scripts/dayrating_store.py rate --score 8 [--note "..."] [--date YYYY-MM-DD]
        (rating a day again edits it; an omitted --note keeps the existing note)
    python scripts/dayrating_store.py show [--date YYYY-MM-DD]
    python scripts/dayrating_store.py list [--days 60]
    python scripts/dayrating_store.py delete --date YYYY-MM-DD
    python scripts/dayrating_store.py status
    python scripts/dayrating_store.py nudge
        ({"nudge": true} when today is not rated yet)

    python scripts/dayrating_store.py insights refresh [--days 90] [--source NAME]
    python scripts/dayrating_store.py insights stats [--days 90]
    python scripts/dayrating_store.py insights sources
    python scripts/dayrating_store.py insights save --narrative "..." [--days 90]
    python scripts/dayrating_store.py insights latest

    python scripts/dayrating_store.py calendar sync --start YYYY-MM-DD --end YYYY-MM-DD --events-file PATH|-
    python scripts/dayrating_store.py calendar status

All commands print JSON to stdout. Errors are {"error": ...} on stderr with exit code 1
(not found) or 2 (bad input).
"""

from __future__ import annotations

import argparse
import json
import sys

import calendar_load
import dayrating
import insights

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("rate")
    p.add_argument("--score", required=True)
    p.add_argument("--note")
    p.add_argument("--date")
    p = sub.add_parser("show")
    p.add_argument("--date")
    p = sub.add_parser("list")
    p.add_argument("--days", type=int, default=60)
    p = sub.add_parser("delete")
    p.add_argument("--date", required=True)
    sub.add_parser("status")
    sub.add_parser("nudge")
    ins = sub.add_parser("insights").add_subparsers(dest="icmd", required=True)
    p = ins.add_parser("refresh")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--source")
    p = ins.add_parser("stats")
    p.add_argument("--days", type=int, default=90)
    ins.add_parser("sources")
    p = ins.add_parser("save")
    p.add_argument("--narrative", required=True)
    p.add_argument("--days", type=int, default=90)
    ins.add_parser("latest")
    cal = sub.add_parser("calendar").add_subparsers(dest="ccmd", required=True)
    p = cal.add_parser("sync")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--events-file", required=True, help="JSON list of events, or - for stdin")
    cal.add_parser("status")
    return ap


def run(args) -> int:
    if args.cmd == "rate":
        _print(dayrating.set_rating(args.score, args.note, rating_date=args.date))
    elif args.cmd == "show":
        r = dayrating.get_rating(args.date)
        if r is None:
            raise dayrating.RatingError("no rating for that day", 1)
        _print(r)
    elif args.cmd == "list":
        _print(dayrating.list_ratings(days=args.days))
    elif args.cmd == "delete":
        _print(dayrating.delete_rating(args.date))
    elif args.cmd == "status":
        _print(dayrating.status())
    elif args.cmd == "nudge":
        _print(dayrating.needs_nudge())
    elif args.cmd == "calendar":
        if args.ccmd == "status":
            _print(calendar_load.status())
        else:
            try:
                raw = sys.stdin.read() if args.events_file == "-" else open(args.events_file, encoding="utf-8").read()
                events = json.loads(raw)
            except (OSError, ValueError) as exc:
                raise insights.InsightError(f"cannot read events: {exc}", 2)
            if not isinstance(events, list):
                raise insights.InsightError("events must be a JSON list", 2)
            _print(calendar_load.sync(events, args.start, args.end))
    elif args.icmd == "refresh":
        _print(insights.refresh(days=args.days, only=args.source))
    elif args.icmd == "stats":
        _print(insights.analyze(days=args.days))
    elif args.icmd == "sources":
        _print(insights.list_sources())
    elif args.icmd == "save":
        _print(insights.save_report(args.narrative, days=args.days))
    elif args.icmd == "latest":
        _print(insights.latest_report() or {"report": None})
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except (dayrating.RatingError, insights.InsightError) as exc:
        print(json.dumps({"error": exc.message}), file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
