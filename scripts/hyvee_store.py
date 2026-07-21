#!/usr/bin/env python3
"""Hy-Vee cart-builder store CLI for the hyvee subagent (stdlib only).

Owns purchase-history snapshot, learned item->product prefs, feedback log, and
cart-run log in the shared SQLite DB (state/agent_results.db, schema in
state/schema.sql). All commands print JSON to stdout. No browser logic here.
"""
from __future__ import annotations
import argparse, json, os, sqlite3, sys, uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "state" / "schema.sql"

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8")

def _db_path() -> Path:
    env = os.environ.get("HYVEE_DB_PATH")
    return Path(env) if env else REPO_ROOT / "state" / "agent_results.db"

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))

def cmd_prefs_list(args) -> None:
    conn = _connect()
    rows = conn.execute("SELECT * FROM hyvee_item_prefs ORDER BY item").fetchall()
    _out([dict(r) for r in rows])

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="group", required=True)
    prefs = sub.add_parser("prefs").add_subparsers(dest="action", required=True)
    prefs.add_parser("list").set_defaults(func=cmd_prefs_list)
    args = ap.parse_args(argv)
    args.func(args)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
