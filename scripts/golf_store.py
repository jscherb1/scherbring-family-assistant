#!/usr/bin/env python3
"""Golf handicap CLI for the golf subagent.

A thin wrapper over scripts/golf.py (the shared service the dashboard uses too), so the
agent and the web UI run the exact same code. Zero-dependency (Python stdlib only).

Usage:
    python scripts/golf_store.py handicap
        (current handicap, which rounds count, best score, average of the last 10)
    python scripts/golf_store.py round list [--limit 20]
    python scripts/golf_store.py round preview --date YYYY-MM-DD --course "Oaks - Whites" --score 88 \
        [--rating 69 --slope 125]
        (what logging it would do to the handicap; writes nothing)
    python scripts/golf_store.py round add --date YYYY-MM-DD --course "..." --score 88 \
        [--rating R --slope S] [--tee Whites] [--notes "..."] [--no-sheet]
        (rating/slope default from the saved course; also appends to the Google Sheet)
    python scripts/golf_store.py round update --id <id> [--date ... --course ... --score N --rating R --slope S --tee ... --notes ...]
    python scripts/golf_store.py round undo
        (removes the most recently entered round)
    python scripts/golf_store.py round delete --id <id>

    python scripts/golf_store.py course list
    python scripts/golf_store.py course add --name "Oaks - Whites" --rating 69 --slope 125 [--tee Whites]

    python scripts/golf_store.py import --from-sheet        (one-time load of the tab's history)
    python scripts/golf_store.py import --csv rounds.csv    (date,course,rating,slope,score)
    python scripts/golf_store.py sync                       (retry rounds not yet in the sheet)
    python scripts/golf_store.py config get|set --key K --value V

All commands print JSON to stdout. Errors are {"error": ...} on stderr with exit code 1
(not found) or 2 (bad input). Only 18-hole rounds are supported.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys

import golf

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_handicap(_a) -> int:
    _print(golf.get_handicap())
    return 0


def cmd_round_list(a) -> int:
    _print(golf.list_rounds(limit=a.limit))
    return 0


def cmd_round_preview(a) -> int:
    _print(golf.preview_round(a.date, a.course, a.score, rating=a.rating, slope=a.slope))
    return 0


def cmd_round_add(a) -> int:
    _print(golf.add_round(a.date, a.course, a.score, rating=a.rating, slope=a.slope, tee=a.tee,
                          notes=a.notes, mirror=not a.no_sheet))
    return 0


def cmd_round_update(a) -> int:
    changes = {"played_on": a.date, "course": a.course, "score": a.score, "rating": a.rating,
               "slope": a.slope, "tee": a.tee, "notes": a.notes}
    _print(golf.update_round(a.id, **{k: v for k, v in changes.items() if v is not None}))
    return 0


def cmd_round_undo(_a) -> int:
    _print(golf.undo_last())
    return 0


def cmd_round_delete(a) -> int:
    _print(golf.delete_round(a.id))
    return 0


def cmd_course_list(_a) -> int:
    _print(golf.list_courses())
    return 0


def cmd_course_add(a) -> int:
    _print(golf.save_course(a.name, a.rating, a.slope, tee=a.tee))
    return 0


def cmd_import(a) -> int:
    if a.csv:
        with open(a.csv, newline="", encoding="utf-8") as fh:
            rows = [r for r in csv.reader(fh) if r]
        if rows and not rows[0][0][:1].isdigit():
            rows = rows[1:]
    else:
        import golf_sheet
        try:
            rows = golf_sheet.read_rows(golf.get_config())
        except Exception as exc:  # noqa: BLE001
            raise golf.GolfError(f"could not read the sheet: {exc}", 1) from exc
    _print(golf.import_rows(rows))
    return 0


def cmd_sync(_a) -> int:
    _print(golf.sync_pending())
    return 0


def cmd_config_get(_a) -> int:
    _print(golf.get_config())
    return 0


def cmd_config_set(a) -> int:
    _print(golf.set_config(a.key, a.value))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Golf handicap store.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("handicap").set_defaults(func=cmd_handicap)

    rnd = sub.add_parser("round")
    rs = rnd.add_subparsers(dest="subcommand", required=True)

    rl = rs.add_parser("list")
    rl.add_argument("--limit", type=int, default=20)
    rl.set_defaults(func=cmd_round_list)

    for name, fn in (("preview", cmd_round_preview), ("add", cmd_round_add)):
        p = rs.add_parser(name)
        p.add_argument("--date", required=True, help="YYYY-MM-DD (m/d/yy also accepted)")
        p.add_argument("--course", required=True)
        p.add_argument("--score", type=int, required=True)
        p.add_argument("--rating", type=float, default=None)
        p.add_argument("--slope", type=int, default=None)
        if name == "add":
            p.add_argument("--tee", default=None)
            p.add_argument("--notes", default=None)
            p.add_argument("--no-sheet", dest="no_sheet", action="store_true")
        p.set_defaults(func=fn)

    ru = rs.add_parser("update")
    ru.add_argument("--id", required=True)
    ru.add_argument("--date", default=None)
    ru.add_argument("--course", default=None)
    ru.add_argument("--score", type=int, default=None)
    ru.add_argument("--rating", type=float, default=None)
    ru.add_argument("--slope", type=int, default=None)
    ru.add_argument("--tee", default=None)
    ru.add_argument("--notes", default=None)
    ru.set_defaults(func=cmd_round_update)

    rs.add_parser("undo").set_defaults(func=cmd_round_undo)
    rd = rs.add_parser("delete")
    rd.add_argument("--id", required=True)
    rd.set_defaults(func=cmd_round_delete)

    course = sub.add_parser("course")
    cs = course.add_subparsers(dest="subcommand", required=True)
    cs.add_parser("list").set_defaults(func=cmd_course_list)
    ca = cs.add_parser("add")
    ca.add_argument("--name", required=True)
    ca.add_argument("--rating", type=float, required=True)
    ca.add_argument("--slope", type=int, required=True)
    ca.add_argument("--tee", default=None)
    ca.set_defaults(func=cmd_course_add)

    imp = sub.add_parser("import")
    src = imp.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-sheet", dest="from_sheet", action="store_true")
    src.add_argument("--csv", default=None)
    imp.set_defaults(func=cmd_import)

    sub.add_parser("sync").set_defaults(func=cmd_sync)

    cfg = sub.add_parser("config")
    cfs = cfg.add_subparsers(dest="subcommand", required=True)
    cfs.add_parser("get").set_defaults(func=cmd_config_get)
    cset = cfs.add_parser("set")
    cset.add_argument("--key", required=True)
    cset.add_argument("--value", required=True)
    cset.set_defaults(func=cmd_config_set)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except golf.GolfError as exc:
        print(json.dumps({"error": exc.message}), file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
