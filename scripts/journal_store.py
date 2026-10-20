#!/usr/bin/env python3
"""Journal + nightly check-in CLI for the journal subagent.

A thin wrapper over scripts/journal.py (the shared service the dashboard's Wellness check-in and Journal page
use too), so the agent and the web UI run the exact same code. Zero-dependency (Python stdlib only).

Usage:
    python scripts/journal_store.py checkin message
        ({"message": "<text to send>"} or {"message": null} when everything for today is in)
    python scripts/journal_store.py checkin pending [--date YYYY-MM-DD]
    python scripts/journal_store.py checkin record [--date YYYY-MM-DD] [--score N] [--note "..."]
        [--yes alcohol,late_meal] [--no screens_bed] [--rest no|yes] [--habits-done "Stretching,Achilles PT"]
        (--rest answers every question still blank after --yes/--no; habits are logged only if not already)

    python scripts/journal_store.py answer --question alcohol --value yes|no [--date YYYY-MM-DD]
    python scripts/journal_store.py answer --question alcohol --clear [--date YYYY-MM-DD]
    python scripts/journal_store.py day [--date YYYY-MM-DD]
    python scripts/journal_store.py history [--days 28]
    python scripts/journal_store.py impact [--days 90]

    python scripts/journal_store.py question list [--all]
    python scripts/journal_store.py question add --text "Caffeine after 2 PM?" [--slug caffeine_late] [--category ...]
    python scripts/journal_store.py question update --question alcohol [--text ...] [--category ...] [--active 0|1]
    python scripts/journal_store.py question import-habit --habit Alcohol --question alcohol [--keep-habit]

`--question` / `--yes` / `--no` accept a slug, the question text, or a unique fragment. All commands print
JSON to stdout. Errors are {"error": ...} on stderr with exit code 1 (not found) or 2 (bad input).
"""

from __future__ import annotations

import argparse
import json
import sys

import journal

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _list(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("checkin").add_subparsers(dest="ccmd", required=True)
    c.add_parser("message")
    p = c.add_parser("pending")
    p.add_argument("--date")
    p = c.add_parser("record")
    p.add_argument("--date")
    p.add_argument("--score")
    p.add_argument("--note")
    p.add_argument("--yes")
    p.add_argument("--no")
    p.add_argument("--rest")
    p.add_argument("--habits-done")

    p = sub.add_parser("answer")
    p.add_argument("--question", required=True)
    p.add_argument("--value")
    p.add_argument("--clear", action="store_true")
    p.add_argument("--date")
    p = sub.add_parser("day")
    p.add_argument("--date")
    p = sub.add_parser("history")
    p.add_argument("--days", type=int, default=28)
    p = sub.add_parser("impact")
    p.add_argument("--days", type=int, default=90)

    q = sub.add_parser("question").add_subparsers(dest="qcmd", required=True)
    p = q.add_parser("list")
    p.add_argument("--all", action="store_true")
    p = q.add_parser("add")
    p.add_argument("--text", required=True)
    p.add_argument("--slug")
    p.add_argument("--category")
    p = q.add_parser("update")
    p.add_argument("--question", required=True)
    p.add_argument("--text")
    p.add_argument("--category")
    p.add_argument("--active", choices=["0", "1"])
    p = q.add_parser("import-habit")
    p.add_argument("--habit", required=True)
    p.add_argument("--question", required=True)
    p.add_argument("--keep-habit", action="store_true")
    return ap


def run(args) -> int:
    if args.cmd == "checkin":
        if args.ccmd == "message":
            out = journal.checkin_message()
            _print({"message": out["message"]})
        elif args.ccmd == "pending":
            _print(journal.pending(args.date))
        else:
            _print(journal.record(day=args.date, score=args.score, note=args.note, yes=_list(args.yes),
                                  no=_list(args.no), rest=args.rest, habits_done=_list(args.habits_done)))
    elif args.cmd == "answer":
        if args.clear:
            _print(journal.clear_answer(args.question, answer_date=args.date))
        elif args.value is None:
            raise journal.JournalError("give --value yes|no or --clear")
        else:
            _print(journal.set_answer(args.question, args.value, answer_date=args.date))
    elif args.cmd == "day":
        _print(journal.pending(args.date))
    elif args.cmd == "history":
        _print(journal.history(days=args.days))
    elif args.cmd == "impact":
        _print(journal.impact(days=args.days))
    elif args.qcmd == "list":
        _print(journal.list_questions(include_inactive=args.all))
    elif args.qcmd == "add":
        _print(journal.add_question(args.text, slug=args.slug, category=args.category))
    elif args.qcmd == "update":
        _print(journal.update_question(args.question, text=args.text, category=args.category,
                                       active=None if args.active is None else args.active == "1"))
    else:
        _print(journal.import_from_habit(args.habit, args.question, pause_habit=not args.keep_habit))
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except journal.JournalError as exc:
        print(json.dumps({"error": exc.message}), file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
