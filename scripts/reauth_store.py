#!/usr/bin/env python3
"""Remote re-authentication CLI for agents (thin wrapper over scripts/reauth.py).

Usage:
    python scripts/reauth_store.py start drive
    python scripts/reauth_store.py complete drive "<pasted http://localhost/callback?code=... address>"
    python scripts/reauth_store.py verify drive
    python scripts/reauth_store.py hyvee-cookies "<exported cookies, JSON or cookies.txt>"

JSON on stdout. Errors are {"error": ...} on stderr; exit 1 = not possible right now, 2 = bad input.
"""

from __future__ import annotations

import argparse
import json
import sys

import reauth


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("start", "verify"):
        sub.add_parser(name).add_argument("system")
    c = sub.add_parser("complete")
    c.add_argument("system")
    c.add_argument("pasted")
    sub.add_parser("hyvee-cookies").add_argument("pasted")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "start":
            out = reauth.start(a.system)
        elif a.cmd == "hyvee-cookies":
            out = reauth.import_hyvee_cookies(a.pasted)
        elif a.cmd == "complete":
            out = reauth.complete(a.system, a.pasted)
        else:
            out = reauth.verify(a.system)
    except reauth.ReauthError as e:
        print(json.dumps({"error": e.message}), file=sys.stderr)
        return e.code
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
