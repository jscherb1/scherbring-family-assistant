#!/usr/bin/env python3
"""Retirement planning CLI for the retirement and finance-advisor subagents.

A thin wrapper over scripts/retirement.py (the shared service), so every caller derives the
inputs and runs the model the same way. Inputs come from the local Monarch snapshot, so no
Monarch tool calls are needed for a run.

--account accepts an account id, an exact name, or a unique name fragment (case-insensitive).
JSON arguments are passed inline, e.g. --allocation '{"us_equity": 0.6, "intl_equity": 0.3, "bonds": 0.1}'.

Usage:
    python scripts/retirement_store.py inputs
        (balances by bucket, the portfolio allocation, trailing savings, sources and warnings)
    python scripts/retirement_store.py accounts list [--bucket pretax]
    python scripts/retirement_store.py accounts classify --account <ref> [--bucket kids] \
        [--allocation '<json>'] [--note "..."] [--clear]
    python scripts/retirement_store.py securities list
    python scripts/retirement_store.py securities set --ticker FDEEX (--allocation '<json>' | --clear)

    python scripts/retirement_store.py config get
    python scripts/retirement_store.py config set --key retirement_spend --value 110000
        (--value is JSON: numbers, objects, lists, or null to remove the key)

    python scripts/retirement_store.py run [--date YYYY-MM-DD] [--paths 10000] [--seed 42] \
        [--override '<json>'] [--scenarios '<json list>'] [--tag whatif]
        (writes state/retirement/{assumptions,result,run}-<date>[-tag].json; never overwrites: a taken
         date+tag gets a -2, -3 ... suffix, and the summary's "tag" is the one actually used)
    python scripts/retirement_store.py workbook --date YYYY-MM-DD [--tag ...] \
        --headline "..." --body "..." --out state/retirement/Retirement-Model-<date>[-tag].xlsx
        (refuses to overwrite an existing --out file)

All commands print JSON to stdout. Errors are {"error": ...} on stderr with exit code 1
(not found) or 2 (bad input).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import retirement
import retirement_workbook

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _json_arg(text: str | None, what: str):
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        raise retirement.RetirementError(f"{what} must be valid JSON") from None


def _date(text: str | None) -> date | None:
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise retirement.RetirementError(f"invalid date {text!r}, expected YYYY-MM-DD") from None


def cmd_inputs(args) -> int:
    _print(retirement.derive_inputs())
    return 0


def cmd_accounts_list(args) -> int:
    rows = retirement.classify_accounts()
    if args.bucket:
        rows = [r for r in rows if r["bucket"] == args.bucket]
    _print(rows)
    return 0


def cmd_accounts_classify(args) -> int:
    _print(retirement.set_account(args.account, bucket=args.bucket,
                                  allocation=_json_arg(args.allocation, "--allocation"),
                                  note=args.note, clear=args.clear))
    return 0


def cmd_securities_list(args) -> int:
    _print({"overrides": retirement.security_overrides(), "known": sorted(retirement.KNOWN_SECURITIES),
            "known_target_date": retirement.KNOWN_TARGET_DATE})
    return 0


def cmd_securities_set(args) -> int:
    if not args.clear and args.allocation is None:
        raise retirement.RetirementError("give --allocation or --clear")
    _print(retirement.set_security(args.ticker, None if args.clear else _json_arg(args.allocation, "--allocation")))
    return 0


def cmd_config_get(args) -> int:
    _print(retirement.get_assumptions())
    return 0


def cmd_config_set(args) -> int:
    _print(retirement.set_assumption(args.key, _json_arg(args.value, "--value")))
    return 0


def cmd_run(args) -> int:
    scenarios = _json_arg(args.scenarios, "--scenarios")
    if scenarios is not None and not isinstance(scenarios, list):
        raise retirement.RetirementError("--scenarios must be a JSON list")
    overrides = _json_arg(args.override, "--override")
    if overrides is not None and not isinstance(overrides, dict):
        raise retirement.RetirementError("--override must be a JSON object")
    _print(retirement.run(today=_date(args.date), n_paths=args.paths, seed=args.seed,
                          overrides=overrides, extra_scenarios=scenarios, tag=args.tag or ""))
    return 0


def cmd_workbook(args) -> int:
    if Path(args.out).exists():
        raise retirement.RetirementError(f"{args.out} already exists; choose a new file name (workbooks are never overwritten)")
    payload = retirement.workbook_payload(args.date, args.headline, args.body, tag=args.tag or "")
    path = retirement_workbook.build_workbook(payload, args.out)
    _print({"workbook_path": path})
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("inputs")
    p.set_defaults(func=cmd_inputs)

    acc = sub.add_parser("accounts").add_subparsers(dest="sub", required=True)
    p = acc.add_parser("list")
    p.add_argument("--bucket", choices=retirement.BUCKETS)
    p.set_defaults(func=cmd_accounts_list)
    p = acc.add_parser("classify")
    p.add_argument("--account", required=True)
    p.add_argument("--bucket", choices=retirement.BUCKETS)
    p.add_argument("--allocation")
    p.add_argument("--note")
    p.add_argument("--clear", action="store_true", help="remove this account's overrides")
    p.set_defaults(func=cmd_accounts_classify)

    sec = sub.add_parser("securities").add_subparsers(dest="sub", required=True)
    p = sec.add_parser("list")
    p.set_defaults(func=cmd_securities_list)
    p = sec.add_parser("set")
    p.add_argument("--ticker", required=True)
    p.add_argument("--allocation")
    p.add_argument("--clear", action="store_true")
    p.set_defaults(func=cmd_securities_set)

    cfg = sub.add_parser("config").add_subparsers(dest="sub", required=True)
    p = cfg.add_parser("get")
    p.set_defaults(func=cmd_config_get)
    p = cfg.add_parser("set")
    p.add_argument("--key", required=True)
    p.add_argument("--value", required=True, help="JSON value; null removes the key")
    p.set_defaults(func=cmd_config_set)

    p = sub.add_parser("run")
    p.add_argument("--date")
    p.add_argument("--paths", type=int, default=10_000)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--override", help="JSON object of assumption overrides for this run only")
    p.add_argument("--scenarios", help="JSON list of extra what-if scenarios for this run")
    p.add_argument("--tag", help="suffix for the run files, e.g. whatif")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("workbook")
    p.add_argument("--date", required=True)
    p.add_argument("--tag")
    p.add_argument("--headline", required=True)
    p.add_argument("--body", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_workbook)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except retirement.RetirementError as exc:
        print(json.dumps({"error": exc.message}), file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
