#!/usr/bin/env python3
"""Kid chore chart CLI for the chores subagent.

A thin wrapper over scripts/chores.py (the shared service the dashboard uses too), so the agent
and the web UI run the exact same code. Zero-dependency (Python stdlib only).

--kid / --chore / --reward accept an id, an exact name, or a unique name fragment (case-insensitive).
Money arguments are dollars ("0.50", "$2"); JSON output carries integer cents plus a formatted string.

Usage:
    python scripts/chores_store.py kid add --name Ruth [--avatar 🦄] [--color "#ff7eb6"]
    python scripts/chores_store.py kid update --kid <ref> [--name ..] [--avatar ..] [--color ..] [--active 0|1]
    python scripts/chores_store.py kid list [--all]

    python scripts/chores_store.py chore add --name "Feed the dog" --frequency daily|weekly [--target N] \
        [--days mon,thu] [--value 0.50] [--icon 🐶] [--description "..."] [--kids Ruth,Claire]
        (weekly + --days = due on those weekdays; weekly without days = N times any day that week)
    python scripts/chores_store.py chore update --chore <ref> [--name ..] [--frequency ..] [--target N] \
        [--days mon,thu | --no-days] [--value D] [--icon ..] [--description ..] [--active 0|1]
    python scripts/chores_store.py chore assign --chore <ref> --kid <ref> [--remove]
    python scripts/chores_store.py chore list [--all]

    python scripts/chores_store.py done --kid <ref> --chore <ref> [--date YYYY-MM-DD]
    python scripts/chores_store.py undo --kid <ref> --chore <ref> [--any-date]
    python scripts/chores_store.py status [--kid <ref>]
        (today's to-do list, progress, streaks, balance, week strip, goal, badges)
    python scripts/chores_store.py balance [--kid <ref>]
    python scripts/chores_store.py ledger --kid <ref> [--limit 50]

    python scripts/chores_store.py payout --kid <ref> --amount D [--note ".."]
    python scripts/chores_store.py adjust --kid <ref> --amount +D|-D [--note ".."]
    python scripts/chores_store.py reward add --name "Movie night" --cost D [--icon 🎬]
    python scripts/chores_store.py reward update --reward <ref> [--name ..] [--cost D] [--icon ..] [--active 0|1]
    python scripts/chores_store.py reward list [--all]
    python scripts/chores_store.py reward redeem --kid <ref> --reward <ref>
    python scripts/chores_store.py goal set --kid <ref> --name "Lego set" --target D
    python scripts/chores_store.py goal clear --kid <ref>

    python scripts/chores_store.py week-summary     (payday numbers per kid)
    python scripts/chores_store.py open [--nudge]    (kids with chores still open today)
    python scripts/chores_store.py config get
    python scripts/chores_store.py config set --key <key> --value <value>

All commands print JSON to stdout. Errors are {"error": ...} on stderr with exit code 1
(not found) or 2 (bad input).
"""

from __future__ import annotations

import argparse
import json
import sys

import chores

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _kid_id(ref: str) -> str:
    return chores.resolve_kid(ref)["id"]


def _chore_id(ref: str) -> str:
    return chores.resolve_chore(ref)["id"]


def _signed_money(text: str) -> int:
    text = text.strip()
    sign = -1 if text.startswith("-") else 1
    return sign * chores.parse_money(text.lstrip("+-"))


def cmd_kid_add(args) -> int:
    _print(chores.add_kid(args.name, avatar=args.avatar, color=args.color))
    return 0


def cmd_kid_update(args) -> int:
    changes = {k: getattr(args, k) for k in ("name", "avatar", "color") if getattr(args, k) is not None}
    if args.active is not None:
        changes["active"] = bool(args.active)
    _print(chores.update_kid(_kid_id(args.kid), **changes))
    return 0


def cmd_kid_list(args) -> int:
    _print(chores.list_kids(include_inactive=args.all))
    return 0


def cmd_chore_add(args) -> int:
    kids = [k.strip() for k in args.kids.split(",") if k.strip()] if args.kids else None
    _print(chores.create_chore(
        args.name, args.frequency, args.target, value_cents=chores.parse_money(args.value), icon=args.icon,
        description=args.description, days=args.days, kids=kids))
    return 0


def cmd_chore_update(args) -> int:
    changes: dict = {}
    for key in ("name", "frequency", "icon", "description"):
        if getattr(args, key) is not None:
            changes[key] = getattr(args, key)
    if args.target is not None:
        changes["target_count"] = args.target
    if args.days is not None and args.no_days:
        raise chores.ChoreError("use either --days or --no-days, not both")
    if args.days is not None:
        changes["days"] = args.days
    if args.no_days:
        changes["days"] = []
    if args.value is not None:
        changes["value_cents"] = chores.parse_money(args.value)
    if args.active is not None:
        changes["active"] = bool(args.active)
    _print(chores.update_chore(_chore_id(args.chore), **changes))
    return 0


def cmd_chore_assign(args) -> int:
    _print(chores.assign(_chore_id(args.chore), _kid_id(args.kid), assigned=not args.remove))
    return 0


def cmd_chore_list(args) -> int:
    _print(chores.list_chores(include_inactive=args.all))
    return 0


def cmd_done(args) -> int:
    _print(chores.complete(_kid_id(args.kid), _chore_id(args.chore), log_date=args.date))
    return 0


def cmd_undo(args) -> int:
    _print(chores.undo(_kid_id(args.kid), _chore_id(args.chore), any_date=args.any_date))
    return 0


def cmd_status(args) -> int:
    _print(chores.kid_view(_kid_id(args.kid)) if args.kid else chores.all_kid_views())
    return 0


def cmd_balance(args) -> int:
    kids = [chores.resolve_kid(args.kid)] if args.kid else chores.list_kids()
    _print([{"kid": k["name"], "balance_cents": chores.balance(k["id"]),
             "balance": chores.money(chores.balance(k["id"]))} for k in kids])
    return 0


def cmd_ledger(args) -> int:
    _print(chores.ledger(_kid_id(args.kid), limit=args.limit))
    return 0


def cmd_payout(args) -> int:
    _print(chores.payout(_kid_id(args.kid), chores.parse_money(args.amount), note=args.note))
    return 0


def cmd_adjust(args) -> int:
    _print(chores.adjust(_kid_id(args.kid), _signed_money(args.amount), note=args.note))
    return 0


def cmd_reward_add(args) -> int:
    _print(chores.create_reward(args.name, chores.parse_money(args.cost), icon=args.icon))
    return 0


def cmd_reward_update(args) -> int:
    changes = {k: getattr(args, k) for k in ("name", "icon") if getattr(args, k) is not None}
    if args.cost is not None:
        changes["cost_cents"] = chores.parse_money(args.cost)
    if args.active is not None:
        changes["active"] = bool(args.active)
    _print(chores.update_reward(chores.resolve_reward(args.reward)["id"], **changes))
    return 0


def cmd_reward_list(args) -> int:
    _print(chores.list_rewards(include_inactive=args.all))
    return 0


def cmd_reward_redeem(args) -> int:
    _print(chores.redeem(_kid_id(args.kid), chores.resolve_reward(args.reward)["id"]))
    return 0


def cmd_goal_set(args) -> int:
    _print(chores.set_goal(_kid_id(args.kid), args.name, chores.parse_money(args.target)))
    return 0


def cmd_goal_clear(args) -> int:
    chores.clear_goal(_kid_id(args.kid))
    _print({"cleared": True})
    return 0


def cmd_week_summary(args) -> int:
    _print(chores.week_summary())
    return 0


def cmd_open(args) -> int:
    _print(chores.open_today() if args.nudge else [
        {"kid": v["kid"]["name"], "remaining": [i["name"] for i in v["todo"] if not i["done"]],
         "done_count": v["done_count"], "total_count": v["total_count"]}
        for v in chores.all_kid_views()])
    return 0


def cmd_config_get(args) -> int:
    _print(chores.get_config())
    return 0


def cmd_config_set(args) -> int:
    _print(chores.set_config(args.key, args.value))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kid chore chart store.")
    sub = parser.add_subparsers(dest="command", required=True)

    def group(name, help_):
        return sub.add_parser(name, help=help_).add_subparsers(dest="subcommand", required=True)

    ks = group("kid", "Kids on the chart.")
    p = ks.add_parser("add")
    p.add_argument("--name", required=True)
    p.add_argument("--avatar", default=None)
    p.add_argument("--color", default=None)
    p.set_defaults(func=cmd_kid_add)
    p = ks.add_parser("update")
    p.add_argument("--kid", required=True)
    p.add_argument("--name", default=None)
    p.add_argument("--avatar", default=None)
    p.add_argument("--color", default=None)
    p.add_argument("--active", type=int, choices=[0, 1], default=None)
    p.set_defaults(func=cmd_kid_update)
    p = ks.add_parser("list")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_kid_list)

    cs = group("chore", "Chore definitions and assignment.")
    p = cs.add_parser("add")
    p.add_argument("--name", required=True)
    p.add_argument("--frequency", choices=chores.FREQUENCIES, default="daily")
    p.add_argument("--target", type=int, default=None)
    p.add_argument("--days", default=None, help="Weekly only, e.g. mon,thu.")
    p.add_argument("--value", default="0", help="Dollars earned per completion.")
    p.add_argument("--icon", default=None)
    p.add_argument("--description", default=None)
    p.add_argument("--kids", default=None, help="Comma-separated kid names to assign.")
    p.set_defaults(func=cmd_chore_add)
    p = cs.add_parser("update")
    p.add_argument("--chore", required=True)
    p.add_argument("--name", default=None)
    p.add_argument("--frequency", choices=chores.FREQUENCIES, default=None)
    p.add_argument("--target", type=int, default=None)
    p.add_argument("--days", default=None)
    p.add_argument("--no-days", dest="no_days", action="store_true")
    p.add_argument("--value", default=None)
    p.add_argument("--icon", default=None)
    p.add_argument("--description", default=None)
    p.add_argument("--active", type=int, choices=[0, 1], default=None)
    p.set_defaults(func=cmd_chore_update)
    p = cs.add_parser("assign")
    p.add_argument("--chore", required=True)
    p.add_argument("--kid", required=True)
    p.add_argument("--remove", action="store_true")
    p.set_defaults(func=cmd_chore_assign)
    p = cs.add_parser("list")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_chore_list)

    p = sub.add_parser("done", help="Record a chore as done.")
    p.add_argument("--kid", required=True)
    p.add_argument("--chore", required=True)
    p.add_argument("--date", default=None, help="YYYY-MM-DD backfill (base pay only, no bonuses).")
    p.set_defaults(func=cmd_done)
    p = sub.add_parser("undo", help="Remove the latest completion (today's unless --any-date).")
    p.add_argument("--kid", required=True)
    p.add_argument("--chore", required=True)
    p.add_argument("--any-date", dest="any_date", action="store_true")
    p.set_defaults(func=cmd_undo)
    p = sub.add_parser("status", help="Today's to-do list, progress, streaks, balance.")
    p.add_argument("--kid", default=None)
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("balance")
    p.add_argument("--kid", default=None)
    p.set_defaults(func=cmd_balance)
    p = sub.add_parser("ledger")
    p.add_argument("--kid", required=True)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_ledger)

    p = sub.add_parser("payout", help="Record real money paid to a kid.")
    p.add_argument("--kid", required=True)
    p.add_argument("--amount", required=True)
    p.add_argument("--note", default=None)
    p.set_defaults(func=cmd_payout)
    p = sub.add_parser("adjust", help="Signed manual correction, e.g. --amount -0.50.")
    p.add_argument("--kid", required=True)
    p.add_argument("--amount", required=True)
    p.add_argument("--note", default=None)
    p.set_defaults(func=cmd_adjust)

    rs = group("reward", "Reward shop.")
    p = rs.add_parser("add")
    p.add_argument("--name", required=True)
    p.add_argument("--cost", required=True)
    p.add_argument("--icon", default=None)
    p.set_defaults(func=cmd_reward_add)
    p = rs.add_parser("update")
    p.add_argument("--reward", required=True)
    p.add_argument("--name", default=None)
    p.add_argument("--cost", default=None)
    p.add_argument("--icon", default=None)
    p.add_argument("--active", type=int, choices=[0, 1], default=None)
    p.set_defaults(func=cmd_reward_update)
    p = rs.add_parser("list")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_reward_list)
    p = rs.add_parser("redeem")
    p.add_argument("--kid", required=True)
    p.add_argument("--reward", required=True)
    p.set_defaults(func=cmd_reward_redeem)

    gs = group("goal", "Savings goal.")
    p = gs.add_parser("set")
    p.add_argument("--kid", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--target", required=True)
    p.set_defaults(func=cmd_goal_set)
    p = gs.add_parser("clear")
    p.add_argument("--kid", required=True)
    p.set_defaults(func=cmd_goal_clear)

    p = sub.add_parser("week-summary", help="Payday numbers per kid.")
    p.set_defaults(func=cmd_week_summary)
    p = sub.add_parser("open", help="Chores still open today.")
    p.add_argument("--nudge", action="store_true", help="Apply the nudge switch; empty when off or all done.")
    p.set_defaults(func=cmd_open)

    cfg = group("config", "Bonus and nudge settings.")
    p = cfg.add_parser("get")
    p.set_defaults(func=cmd_config_get)
    p = cfg.add_parser("set")
    p.add_argument("--key", required=True)
    p.add_argument("--value", required=True)
    p.set_defaults(func=cmd_config_set)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except chores.ChoreError as exc:
        print(json.dumps({"error": exc.message}), file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
