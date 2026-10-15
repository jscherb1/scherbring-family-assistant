#!/usr/bin/env python3
"""My Hub CLI for the myhub subagent.

A thin wrapper over scripts/myhub.py (the shared service the dashboard's /me page uses too), so the
agent and the web UI run the exact same code. Zero-dependency (Python stdlib only).

Usage:
    python scripts/myhub_store.py summary
        (everything: needs-attention list, every card; what the /me page shows)
    python scripts/myhub_store.py attention
        (only the ranked needs-attention list plus counts)
    python scripts/myhub_store.py snapshot set --kind event --start YYYY-MM-DD --end YYYY-MM-DD --items-file PATH|-
    python scripts/myhub_store.py snapshot set --kind task --items-file PATH|-
        (items: JSON list of {"title","day","start_time","end_time","source","detail","priority"};
         events replace that date range, tasks replace all stored tasks)
    python scripts/myhub_store.py snapshot status

All commands print JSON to stdout. Errors are {"error": ...} on stderr with exit code 1
(not found) or 2 (bad input).
"""

from __future__ import annotations

import argparse
import json
import sys

import myhub

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def _out(obj) -> int:
    print(json.dumps(obj, indent=2, default=str))
    return 0


def run(args) -> int:
    if args.cmd == "summary":
        return _out(myhub.get_hub())
    if args.cmd == "attention":
        h = myhub.get_hub()
        return _out({"date": h["date"], "counts": h["counts"], "attention": h["attention"], "groups": h["groups"], "errors": h["errors"]})
    if args.snap == "status":
        return _out(myhub.snapshot_status())
    raw = sys.stdin.read() if args.items_file == "-" else open(args.items_file, encoding="utf-8-sig").read()
    try:
        items = json.loads(raw)
    except ValueError as exc:
        raise myhub.HubError(f"items file is not valid JSON: {exc}") from None
    return _out(myhub.set_snapshot(args.kind, items, start=args.start, end=args.end))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("summary")
    sub.add_parser("attention")
    sn = sub.add_parser("snapshot")
    ss = sn.add_subparsers(dest="snap", required=True)
    st = ss.add_parser("set")
    st.add_argument("--kind", required=True, choices=myhub.KINDS)
    st.add_argument("--start")
    st.add_argument("--end")
    st.add_argument("--items-file", required=True)
    ss.add_parser("status")
    args = p.parse_args(argv)
    try:
        return run(args)
    except myhub.HubError as exc:
        print(json.dumps({"error": exc.message}), file=sys.stderr)
        return exc.code
    except OSError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
