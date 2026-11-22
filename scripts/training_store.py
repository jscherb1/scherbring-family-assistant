#!/usr/bin/env python3
"""Training CLI for the training subagent: running volume and heart-rate-zone time from COROS.

A thin wrapper over scripts/training.py (the shared service the dashboard uses too), so the agent and
the web UI run the exact same code. Zero-dependency (Python stdlib only).

Usage:
    python scripts/training_store.py sync [--all-sports] [--full]
        (live COROS fetch; exit 3 when some activities failed but the rest were saved)
    python scripts/training_store.py sports
    python scripts/training_store.py volume [--metric distance|time] [--bucket week] [--sports running] [--start D] [--end D]
    python scripts/training_store.py zones [--bucket total|day|week|month] [--sports ...] [--start D] [--end D]
    python scripts/training_store.py share [--bucket ...] ...      (percent per zone)
    python scripts/training_store.py easy [--bucket ...] ...       (easy = zones 0-2, plus Z1-Z2 only)
    python scripts/training_store.py longest [--sports ...] [--start D] [--end D]
    python scripts/training_store.py summary [--sports ...] [--start D] [--end D]
    python scripts/training_store.py activities [--sports ...] [--start D] [--end D] [--limit 50]
    python scripts/training_store.py zone-defs
    python scripts/training_store.py config get
    python scripts/training_store.py config set --key default_sports --value running,cycling

--sports takes group names (running, walking, cycling, hiking, strength, other, all) and/or COROS
sport ids, comma separated; default is the configured default_sports. Dates are YYYY-MM-DD.
All commands print JSON to stdout. Errors are {"error": ...} on stderr with exit code 1 (not found),
2 (bad input) or 3 (COROS sync failure).
"""

from __future__ import annotations

import argparse
import json
import sys

import training

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _range(args) -> dict:
    return {"sports": args.sports, "start": args.start, "end": args.end}


def cmd_sync(args) -> int:
    result = training.sync(all_sports=args.all_sports, full=args.full)
    _print(result)
    return 3 if result["partial"] else 0


def cmd_sports(args) -> int:
    _print(training.sports_available())
    return 0


def cmd_volume(args) -> int:
    _print(training.volume(args.metric, args.bucket, **_range(args)))
    return 0


def cmd_zones(args) -> int:
    _print(training.zone_time(args.bucket, **_range(args)))
    return 0


def cmd_share(args) -> int:
    _print(training.zone_share(args.bucket, **_range(args)))
    return 0


def cmd_easy(args) -> int:
    _print(training.easy_share(args.bucket, **_range(args)))
    return 0


def cmd_longest(args) -> int:
    _print(training.longest_run_per_week(**_range(args)))
    return 0


def cmd_summary(args) -> int:
    _print(training.summary(**_range(args)))
    return 0


def cmd_activities(args) -> int:
    _print(training.activities(limit=args.limit, **_range(args)))
    return 0


def cmd_zone_defs(args) -> int:
    _print(training.zone_definitions())
    return 0


def cmd_config(args) -> int:
    if args.action == "set":
        if not args.key or args.value is None:
            raise training.TrainingError("config set needs --key and --value")
        _print(training.set_config(args.key, args.value))
    else:
        _print(training.get_config())
    return 0


def _common(p, bucket: str | None = None, buckets=training.BUCKETS) -> None:
    p.add_argument("--sports", help="groups and/or sport ids, comma separated (default: configured)")
    p.add_argument("--start", help="YYYY-MM-DD")
    p.add_argument("--end", help="YYYY-MM-DD")
    if bucket:
        p.add_argument("--bucket", default=bucket, choices=buckets)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="COROS training volume and heart-rate zones.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sync")
    p.add_argument("--all-sports", action="store_true", help="fetch zone detail for every sport")
    p.add_argument("--full", action="store_true", help="re-list all history, not just the recent window")
    p.set_defaults(fn=cmd_sync)

    sub.add_parser("sports").set_defaults(fn=cmd_sports)

    p = sub.add_parser("volume")
    p.add_argument("--metric", default="distance", choices=training.METRICS)
    _common(p, "week", ("week",))
    p.set_defaults(fn=cmd_volume)

    for name, fn, default in (("zones", cmd_zones, "total"), ("share", cmd_share, "total"),
                              ("easy", cmd_easy, "week")):
        p = sub.add_parser(name)
        _common(p, default)
        p.set_defaults(fn=fn)

    for name, fn in (("longest", cmd_longest), ("summary", cmd_summary)):
        p = sub.add_parser(name)
        _common(p)
        p.set_defaults(fn=fn)

    p = sub.add_parser("activities")
    _common(p)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(fn=cmd_activities)

    sub.add_parser("zone-defs").set_defaults(fn=cmd_zone_defs)

    p = sub.add_parser("config")
    p.add_argument("action", choices=["get", "set"])
    p.add_argument("--key")
    p.add_argument("--value")
    p.set_defaults(fn=cmd_config)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except training.TrainingError as exc:
        print(json.dumps({"error": exc.message}), file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
