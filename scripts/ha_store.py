#!/usr/bin/env python3
"""Home Assistant board + health CLI for the ha-overview subagent.

A thin wrapper over scripts/ha.py (the shared service the dashboard uses too), so the agent and
the web UI run the exact same code. Zero-dependency (Python stdlib only).

Usage:
    python scripts/ha_store.py health [--brief]
        (needs-attention list, recommendations, update/unavailable counts; --brief = headline + top items)
    python scripts/ha_store.py board [--section overview|climate|media|security|calendar|batteries|irrigation|holiday]
    python scripts/ha_store.py set-temp --entity climate.claire_thermostat --value 68
    python scripts/ha_store.py set-mode --entity climate.claire_thermostat --value heat|off
    python scripts/ha_store.py toggle --entity input_boolean.kid_door_alerts --value on|off

Only allow-listed entities can be changed (thermostats, kid door alerts, Christmas lights and
automations, media play/pause and volume). Alarm, garage doors and locks are read-only here.
All commands print JSON to stdout. Errors are {"error": ...} on stderr with exit code 1
(not found / unreachable) or 2 (bad input).
"""

from __future__ import annotations

import argparse
import json
import sys

import ha

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_health(args) -> int:
    result = ha.health()
    if args.brief:
        result = {"checked_at": result["checked_at"], "tiles": result["tiles"],
                  "attention": result["attention"][:6], "recommendations": result["recommendations"][:4]}
    _print(result)
    return 0


def cmd_board(args) -> int:
    result = ha.board()
    _print(result[args.section] if args.section else result)
    return 0


def cmd_set_temp(args) -> int:
    _print(ha.act(args.entity, "set_temperature", args.value))
    return 0


def cmd_set_mode(args) -> int:
    _print(ha.act(args.entity, "set_hvac_mode", args.value))
    return 0


def cmd_toggle(args) -> int:
    _print(ha.act(args.entity, "turn_on" if args.value == "on" else "turn_off"))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("health"); s.add_argument("--brief", action="store_true"); s.set_defaults(fn=cmd_health)
    s = sub.add_parser("board"); s.add_argument("--section"); s.set_defaults(fn=cmd_board)
    for name, fn in (("set-temp", cmd_set_temp), ("set-mode", cmd_set_mode)):
        s = sub.add_parser(name); s.add_argument("--entity", required=True); s.add_argument("--value", required=True)
        s.set_defaults(fn=fn)
    s = sub.add_parser("toggle"); s.add_argument("--entity", required=True)
    s.add_argument("--value", required=True, choices=["on", "off"]); s.set_defaults(fn=cmd_toggle)
    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except ha.HAError as exc:
        print(json.dumps({"error": exc.message}), file=sys.stderr)
        return exc.code
    except KeyError:
        print(json.dumps({"error": f"unknown section {args.section!r}"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
