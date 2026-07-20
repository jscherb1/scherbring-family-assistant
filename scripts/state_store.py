#!/usr/bin/env python3
"""Shared state store CLI for the personal-assistant agents.

Zero-dependency (Python stdlib only). Subagents call this via the Bash tool to
persist and recall what they did, which is how follow-up questions get continuity.

Usage:
    python scripts/state_store.py write \
        --agent todoist \
        --task "add 'buy milk' to shopping list" \
        --summary "Created task 'buy milk' in project 'Shopping'." \
        --detail-json '{"task_id":"123","content":"buy milk","project":"Shopping"}'

    python scripts/state_store.py query --agent todoist --limit 5
    python scripts/state_store.py query                      # all agents, recent first

The DB is auto-created from state/schema.sql on first use.
`query` prints a JSON array (newest first) to stdout.
`write` prints the created row as a JSON object to stdout.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Resolve paths relative to the repo root (this file lives in <root>/scripts/).
REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "state" / "agent_results.db"
SCHEMA_PATH = REPO_ROOT / "state" / "schema.sql"

# Task/summary text can contain non-ASCII characters; force UTF-8 on stdout/stderr so
# this never crashes on Windows' default console codepage, regardless of caller env.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def _connect() -> sqlite3.Connection:
    """Open the DB, creating it from schema.sql if it doesn't exist yet."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(schema)
    return conn


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cmd_write(args: argparse.Namespace) -> int:
    detail = args.detail_json
    if detail is not None:
        # Validate it's JSON so we never store malformed detail silently.
        try:
            json.loads(detail)
        except json.JSONDecodeError as exc:
            print(f"error: --detail-json is not valid JSON: {exc}", file=sys.stderr)
            return 2

    created_at = _utc_now_iso()
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO agent_results (agent, task, created_at, summary, detail_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (args.agent, args.task, created_at, args.summary, detail),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM agent_results WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    finally:
        conn.close()

    print(json.dumps(dict(row), ensure_ascii=False, indent=2))
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        sql = "SELECT * FROM agent_results"
        params: list = []
        if args.agent:
            sql += " WHERE agent = ?"
            params.append(args.agent)
        sql += " ORDER BY created_at DESC, id DESC"
        if args.limit is not None:
            sql += " LIMIT ?"
            params.append(args.limit)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Personal-assistant shared state store.")
    sub = parser.add_subparsers(dest="command", required=True)

    w = sub.add_parser("write", help="Insert an agent result row.")
    w.add_argument("--agent", required=True)
    w.add_argument("--task", required=True)
    w.add_argument("--summary", required=True)
    w.add_argument("--detail-json", dest="detail_json", default=None)
    w.set_defaults(func=cmd_write)

    q = sub.add_parser("query", help="Read recent agent result rows (newest first).")
    q.add_argument("--agent", default=None)
    q.add_argument("--limit", type=int, default=None)
    q.set_defaults(func=cmd_query)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
