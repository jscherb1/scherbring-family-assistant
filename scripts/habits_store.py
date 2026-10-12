#!/usr/bin/env python3
"""Habit tracker CLI for the habits subagent.

A thin wrapper over scripts/habits.py (the shared service the dashboard uses too), so the
agent and the web UI run the exact same code. Zero-dependency (Python stdlib only).

--habit accepts a habit id, an exact name, or a unique name fragment (case-insensitive).

Usage:
    python scripts/habits_store.py habit add --name "Stretch" --frequency daily [--target 1] \
        [--owner Justin] [--description "..."] [--no-remind]
        (omit --target for a no-goal habit that is only counted)
    python scripts/habits_store.py habit update --habit <ref> [--name ...] [--frequency ...] \
        [--target N | --no-goal] [--owner X | --shared] [--description ...] \
        [--remind 0|1] [--active 0|1]
    python scripts/habits_store.py habit list [--owner Justin] [--all]
        (every habit with current streak, longest streak, progress this period)
    python scripts/habits_store.py habit show --habit <ref>

    python scripts/habits_store.py log add --habit <ref> [--date YYYY-MM-DD] [--count N] [--note "..."]
    python scripts/habits_store.py log undo --habit <ref>
        (removes the most recently entered log)
    python scripts/habits_store.py log list --habit <ref> [--limit 20]

    python scripts/habits_store.py at-risk [--nudge]
        (habits whose streak breaks if not done soon; --nudge applies the nudge settings)

    python scripts/habits_store.py config get
    python scripts/habits_store.py config set --key nudge_enabled --value 0|1

All commands print JSON to stdout. Errors are {"error": ...} on stderr with exit code 1
(not found) or 2 (bad input).
"""

from __future__ import annotations

import argparse
import json
import sys

import habits

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _habit_id(args) -> str:
    return habits.resolve_habit(args.habit)["id"]


def cmd_habit_add(args) -> int:
    h = habits.create_habit(args.name, args.frequency, args.target, owner=args.owner,
                            description=args.description, remind=not args.no_remind)
    _print(habits.get_status(h["id"]))
    return 0


def cmd_habit_update(args) -> int:
    changes: dict = {}
    for key in ("name", "frequency", "description", "owner"):
        if getattr(args, key) is not None:
            changes[key] = getattr(args, key)
    if args.target is not None and args.no_goal:
        raise habits.HabitError("use either --target or --no-goal, not both")
    if args.target is not None:
        changes["target_count"] = args.target
    if args.no_goal:
        changes["target_count"] = None
    if args.shared:
        changes["owner"] = None
    if args.remind is not None:
        changes["remind"] = bool(args.remind)
    if args.active is not None:
        changes["active"] = bool(args.active)
    h = habits.update_habit(_habit_id(args), **changes)
    _print(habits.get_status(h["id"]))
    return 0


def cmd_habit_list(args) -> int:
    _print(habits.list_status(owner=args.owner, include_inactive=args.all))
    return 0


def cmd_habit_show(args) -> int:
    _print(habits.get_status(_habit_id(args)))
    return 0


def cmd_log_add(args) -> int:
    _print(habits.log_completion(_habit_id(args), log_date=args.date, count=args.count, note=args.note))
    return 0


def cmd_log_undo(args) -> int:
    _print(habits.undo_last(_habit_id(args)))
    return 0


def cmd_log_list(args) -> int:
    _print(habits.list_logs(_habit_id(args), limit=args.limit))
    return 0


def cmd_at_risk(args) -> int:
    _print(habits.get_at_risk(for_nudge=args.nudge))
    return 0


def cmd_config_get(args) -> int:
    _print(habits.get_config())
    return 0


def cmd_config_set(args) -> int:
    _print(habits.set_config(args.key, args.value))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Habit tracker store.")
    sub = parser.add_subparsers(dest="command", required=True)

    habit = sub.add_parser("habit", help="Habit definitions and status.")
    hs = habit.add_subparsers(dest="subcommand", required=True)

    ha = hs.add_parser("add")
    ha.add_argument("--name", required=True)
    ha.add_argument("--frequency", required=True, choices=habits.FREQUENCIES)
    ha.add_argument("--target", type=int, default=None, help="Completions needed per period; omit for no goal.")
    ha.add_argument("--owner", default=None)
    ha.add_argument("--description", default=None)
    ha.add_argument("--no-remind", dest="no_remind", action="store_true")
    ha.set_defaults(func=cmd_habit_add)

    hu = hs.add_parser("update")
    hu.add_argument("--habit", required=True)
    hu.add_argument("--name", default=None)
    hu.add_argument("--frequency", default=None, choices=habits.FREQUENCIES)
    hu.add_argument("--target", type=int, default=None)
    hu.add_argument("--no-goal", dest="no_goal", action="store_true")
    hu.add_argument("--owner", default=None)
    hu.add_argument("--shared", action="store_true", help="Clear the owner.")
    hu.add_argument("--description", default=None)
    hu.add_argument("--remind", type=int, default=None, choices=[0, 1])
    hu.add_argument("--active", type=int, default=None, choices=[0, 1])
    hu.set_defaults(func=cmd_habit_update)

    hl = hs.add_parser("list")
    hl.add_argument("--owner", default=None, help="This owner plus shared habits.")
    hl.add_argument("--all", action="store_true", help="Include paused habits.")
    hl.set_defaults(func=cmd_habit_list)

    hsh = hs.add_parser("show")
    hsh.add_argument("--habit", required=True)
    hsh.set_defaults(func=cmd_habit_show)

    log = sub.add_parser("log", help="Completion log.")
    ls = log.add_subparsers(dest="subcommand", required=True)

    la = ls.add_parser("add")
    la.add_argument("--habit", required=True)
    la.add_argument("--date", default=None, help="YYYY-MM-DD; defaults to today. Past dates are allowed.")
    la.add_argument("--count", type=int, default=1)
    la.add_argument("--note", default=None)
    la.set_defaults(func=cmd_log_add)

    lu = ls.add_parser("undo")
    lu.add_argument("--habit", required=True)
    lu.set_defaults(func=cmd_log_undo)

    ll = ls.add_parser("list")
    ll.add_argument("--habit", required=True)
    ll.add_argument("--limit", type=int, default=20)
    ll.set_defaults(func=cmd_log_list)

    ar = sub.add_parser("at-risk", help="Habits whose streak breaks if not done soon.")
    ar.add_argument("--nudge", action="store_true", help="Apply nudge settings (global switch + per-habit remind).")
    ar.set_defaults(func=cmd_at_risk)

    cfg = sub.add_parser("config", help="Nudge settings.")
    cs = cfg.add_subparsers(dest="subcommand", required=True)
    cg = cs.add_parser("get")
    cg.set_defaults(func=cmd_config_get)
    cset = cs.add_parser("set")
    cset.add_argument("--key", required=True)
    cset.add_argument("--value", required=True)
    cset.set_defaults(func=cmd_config_set)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except habits.HabitError as exc:
        print(json.dumps({"error": exc.message}), file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
