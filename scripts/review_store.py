#!/usr/bin/env python3
"""Weekly step-back review CLI for the review subagent.

A thin wrapper over scripts/review.py (the shared service the dashboard's /review page uses too), so the
agent and the web UI run the exact same code. Zero-dependency (Python stdlib only).

Usage:
    python scripts/review_store.py gather
        (the cross-domain digest, bounded to about 30k characters, plus earlier findings and their statuses)
    python scripts/review_store.py save --file state/review_tmp/<name>.json
        (JSON: {"summary": "...", "findings": [{"key","domain","title","why_it_matters","evidence",
         "suggested_action"}, ...]}; at most 5 findings, and an empty list is a valid run)
    python scripts/review_store.py findings [--status new|accepted|dismissed|done] [--limit 50]
    python scripts/review_store.py status --id ID_OR_KEY --status accepted|dismissed|done|new
    python scripts/review_store.py latest

All commands print JSON to stdout. Errors are {"error": ...} on stderr with exit code 1
(not found) or 2 (bad input).
"""

from __future__ import annotations

import argparse
import json
import sys

import review

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gather")
    p = sub.add_parser("save")
    p.add_argument("--file", required=True, help="JSON file with summary and findings")
    p = sub.add_parser("findings")
    p.add_argument("--status")
    p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("status")
    p.add_argument("--id", required=True, help="finding id or key")
    p.add_argument("--status", required=True)
    sub.add_parser("latest")
    return ap


def run(args) -> int:
    if args.cmd == "gather":
        # compact on purpose: the digest's size bound (about 30k characters) is measured on compact JSON
        print(json.dumps(review.gather(), ensure_ascii=False, separators=(",", ":")))
    elif args.cmd == "save":
        try:
            with open(args.file, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError) as exc:
            raise review.ReviewError(f"cannot read {args.file}: {exc}", 2)
        if not isinstance(payload, dict):
            raise review.ReviewError("the file must hold a JSON object with summary and findings", 2)
        _print(review.save_run(payload.get("summary"), payload.get("findings", [])))
    elif args.cmd == "findings":
        _print(review.list_findings(status=args.status, limit=args.limit))
    elif args.cmd == "status":
        _print(review.set_status(args.id, args.status))
    elif args.cmd == "latest":
        _print(review.latest_run())
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except review.ReviewError as exc:
        print(json.dumps({"error": exc.message}), file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
