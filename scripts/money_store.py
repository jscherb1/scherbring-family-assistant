#!/usr/bin/env python3
"""Money CLI for the finance subagent: budget, spending, net worth and review-queue status.

A thin wrapper over scripts/money.py (the shared service the dashboard's Money pages use too), so the
agent and the web UI read the exact same numbers from the local Monarch snapshot. Zero-dependency
(Python stdlib only). The snapshot is refreshed every 30 min, 6am-11pm, by scripts/money_sync.py;
check `sync-status` and fall back to the live Monarch tools when it's stale.

Usage:
    python scripts/money_store.py overview          (everything the Money home page shows)
    python scripts/money_store.py budget            (fixed / flexible / non-monthly / total vs plan, with status)
    python scripts/money_store.py spending          (month to date vs last month and normal, savings rate)
    python scripts/money_store.py review            (needs-review and no-WHO-tag counts, oldest few of each)
    python scripts/money_store.py sync-status
    python scripts/money_store.py config get
    python scripts/money_store.py config set --key spend_yellow_pct --value 115

All commands print JSON to stdout. Errors are {"error": ...} on stderr with exit code 1
(not found) or 2 (bad input).
"""

from __future__ import annotations

import argparse
import json
import sys

import money

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_overview(args) -> int:
    _print(money.overview())
    return 0


def cmd_budget(args) -> int:
    o = money.overview()
    _print({k: o[k] for k in ("as_of", "month_elapsed_pct", "budget", "projection", "over_budget", "sync")})
    return 0


def cmd_spending(args) -> int:
    o = money.overview()
    _print({k: o[k] for k in ("as_of", "spending", "savings", "bills", "sync")})
    return 0


def cmd_review(args) -> int:
    _print({**money.review_queue(limit=args.limit), "sync": money.sync_status()})
    return 0


def cmd_sync_status(args) -> int:
    _print(money.sync_status())
    return 0


def cmd_config_get(args) -> int:
    _print(money.get_config())
    return 0


def cmd_config_set(args) -> int:
    _print(money.set_config(args.key, args.value))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("overview").set_defaults(func=cmd_overview)
    sub.add_parser("budget").set_defaults(func=cmd_budget)
    sub.add_parser("spending").set_defaults(func=cmd_spending)
    rv = sub.add_parser("review")
    rv.add_argument("--limit", type=int, default=8)
    rv.set_defaults(func=cmd_review)
    sub.add_parser("sync-status").set_defaults(func=cmd_sync_status)
    cfg = sub.add_parser("config").add_subparsers(dest="config_cmd", required=True)
    cfg.add_parser("get").set_defaults(func=cmd_config_get)
    cs = cfg.add_parser("set")
    cs.add_argument("--key", required=True)
    cs.add_argument("--value", required=True)
    cs.set_defaults(func=cmd_config_set)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except money.MoneyError as e:
        print(json.dumps({"error": e.message}), file=sys.stderr)
        return e.code


if __name__ == "__main__":
    raise SystemExit(main())
