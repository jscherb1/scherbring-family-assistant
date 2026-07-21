#!/usr/bin/env python3
"""Weather-reminders CLI for the weather-reminders subagent.

Zero-dependency (Python stdlib only), same SQLite file as state_store.py
(state/agent_results.db, schema in state/schema.sql). The `weather-reminders`
subagent uses this to store the home location/coordinates and alert
thresholds used for its Open-Meteo forecast lookup, and to log proactive
alerts already sent so the daily check doesn't repeat the same rain/snow/
storm event on consecutive mornings.

Usage:
    python scripts/weather_store.py config set --key latitude --value 44.0234
    python scripts/weather_store.py config get --key latitude
    python scripts/weather_store.py config list

    python scripts/weather_store.py alert log --type rain_cushions --target-date 2026-07-22 \
        [--todoist-task-created]
    python scripts/weather_store.py alert check --type rain_cushions --target-date 2026-07-22
        ({"already_alerted": true|false})
    python scripts/weather_store.py alert list [--since 2026-01-01]

All commands print JSON to stdout.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "state" / "agent_results.db"
SCHEMA_PATH = REPO_ROOT / "state" / "schema.sql"

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

ALERT_TYPES = ["rain_cushions", "snow_shoveling", "severe_weather"]


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def _local_now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _require_date(value: str, field: str) -> int:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        print(json.dumps({"error": f"invalid {field} {value!r}, expected YYYY-MM-DD"}), file=sys.stderr)
        return 2
    return 0


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def cmd_config_set(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO weather_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (args.key, args.value),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM weather_config WHERE key = ?", (args.key,)).fetchone()
    finally:
        conn.close()
    _print(dict(row))
    return 0


def cmd_config_get(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM weather_config WHERE key = ?", (args.key,)).fetchone()
    finally:
        conn.close()
    if row is None:
        print(json.dumps({"error": f"no config key {args.key}"}), file=sys.stderr)
        return 1
    _print(dict(row))
    return 0


def cmd_config_list(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM weather_config ORDER BY key").fetchall()
    finally:
        conn.close()
    _print([dict(r) for r in rows])
    return 0


# ---------------------------------------------------------------------------
# alert
# ---------------------------------------------------------------------------


def cmd_alert_log(args: argparse.Namespace) -> int:
    rc = _require_date(args.target_date, "--target-date")
    if rc:
        return rc
    new_id = str(uuid.uuid4())
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO weather_alerts (id, alert_type, target_date, todoist_task_created, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (new_id, args.type, args.target_date, 1 if args.todoist_task_created else 0, _local_now_iso()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM weather_alerts WHERE alert_type = ? AND target_date = ?",
            (args.type, args.target_date),
        ).fetchone()
    finally:
        conn.close()
    _print(dict(row))
    return 0


def cmd_alert_check(args: argparse.Namespace) -> int:
    rc = _require_date(args.target_date, "--target-date")
    if rc:
        return rc
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM weather_alerts WHERE alert_type = ? AND target_date = ?",
            (args.type, args.target_date),
        ).fetchone()
    finally:
        conn.close()
    _print({"already_alerted": row is not None})
    return 0


def cmd_alert_list(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        sql = "SELECT * FROM weather_alerts WHERE 1=1"
        params: list = []
        if args.since:
            sql += " AND target_date >= ?"
            params.append(args.since)
        sql += " ORDER BY target_date DESC, created_at DESC"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    _print([dict(r) for r in rows])
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Weather-reminders store.")
    sub = parser.add_subparsers(dest="command", required=True)

    config = sub.add_parser("config", help="Location/coordinates + alert threshold key/value config.")
    config_sub = config.add_subparsers(dest="subcommand", required=True)

    cs = config_sub.add_parser("set")
    cs.add_argument("--key", required=True)
    cs.add_argument("--value", required=True)
    cs.set_defaults(func=cmd_config_set)

    cg = config_sub.add_parser("get")
    cg.add_argument("--key", required=True)
    cg.set_defaults(func=cmd_config_get)

    cl = config_sub.add_parser("list")
    cl.set_defaults(func=cmd_config_list)

    alert = sub.add_parser("alert", help="Proactive alert dedup log.")
    alert_sub = alert.add_subparsers(dest="subcommand", required=True)

    al = alert_sub.add_parser("log")
    al.add_argument("--type", required=True, choices=ALERT_TYPES)
    al.add_argument("--target-date", dest="target_date", required=True, help="YYYY-MM-DD")
    al.add_argument("--todoist-task-created", dest="todoist_task_created", action="store_true")
    al.set_defaults(func=cmd_alert_log)

    ac = alert_sub.add_parser("check")
    ac.add_argument("--type", required=True, choices=ALERT_TYPES)
    ac.add_argument("--target-date", dest="target_date", required=True, help="YYYY-MM-DD")
    ac.set_defaults(func=cmd_alert_check)

    ali = alert_sub.add_parser("list")
    ali.add_argument("--since", default=None, help="YYYY-MM-DD")
    ali.set_defaults(func=cmd_alert_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
