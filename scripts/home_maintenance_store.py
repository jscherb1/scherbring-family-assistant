#!/usr/bin/env python3
"""Home maintenance CLI for the home-maintenance subagent.

Zero-dependency (Python stdlib only), same SQLite file as state_store.py
(state/agent_results.db, schema in state/schema.sql). The `home-maintenance`
subagent uses this to track recurring indoor maintenance items (HVAC filters,
smoke detector batteries, water softener salt, etc.), a log of completions, and
what's due for the weekly proactive check.

Usage:
    python scripts/home_maintenance_store.py item add --name "HVAC filter change" \
        --category hvac --interval-days 90 [--notes "..."] [--last-done-date 2026-07-01]
    python scripts/home_maintenance_store.py item update --id <id> [--name ...] \
        [--category ...] [--interval-days ...] [--active 0|1] [--notes ...]
    python scripts/home_maintenance_store.py item list [--category hvac] [--active-only]
    python scripts/home_maintenance_store.py item seed
        (one-time insert of the starter item list; no-op if items already exist)

    python scripts/home_maintenance_store.py completion add --item-id <id> \
        [--completed-date 2026-07-20] [--notes "..."]
    python scripts/home_maintenance_store.py completion list --item-id <id> [--limit 20]
    python scripts/home_maintenance_store.py completion last --item-id <id>
        (most recent completion, or {} if none)

    python scripts/home_maintenance_store.py due list [--horizon-days 7]
        (active items overdue or due within the horizon, with last_done_date,
        next_due_date, and days_until_due computed per item)

All commands print JSON to stdout.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "state" / "agent_results.db"
SCHEMA_PATH = REPO_ROOT / "state" / "schema.sql"

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

STARTER_ITEMS = [
    dict(name="HVAC filter change", category="hvac", interval_days=90,
         notes="Default cadence for a standard pleated filter; adjust once you know your filter type/size."),
    dict(name="Smoke/CO detector battery test", category="safety", interval_days=182, notes=None),
    dict(name="Water softener salt check", category="appliance", interval_days=30, notes=None),
    dict(name="Garage clean-out", category="cleaning", interval_days=182, notes="Spring and fall."),
    dict(name="Winterize outdoor spigots/hose bibs", category="seasonal", interval_days=365,
         notes="Ahead of freezing temps."),
    dict(name="Test GFCI outlets", category="safety", interval_days=365, notes=None),
]


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def _local_now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _require_date(value: str, field: str) -> int:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        print(json.dumps({"error": f"invalid {field} {value!r}, expected YYYY-MM-DD"}), file=sys.stderr)
        return 2
    return 0


def _row_to_dict(row: sqlite3.Row, json_fields: tuple[str, ...] = ()) -> dict:
    d = dict(row)
    for key in json_fields:
        if d.get(key):
            d[key] = json.loads(d[key])
    return d


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# item
# ---------------------------------------------------------------------------


def cmd_item_add(args: argparse.Namespace) -> int:
    if args.last_done_date is not None:
        rc = _require_date(args.last_done_date, "--last-done-date")
        if rc:
            return rc
    now = _local_now_iso()
    new_id = str(uuid.uuid4())
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO home_maintenance_items (
                id, name, category, interval_days, active, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (new_id, args.name, args.category, args.interval_days, args.notes, now, now),
        )
        if args.last_done_date is not None:
            conn.execute(
                """
                INSERT INTO home_maintenance_completions (id, item_id, completed_date, notes, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), new_id, args.last_done_date, None, now),
            )
        conn.commit()
        row = conn.execute("SELECT * FROM home_maintenance_items WHERE id = ?", (new_id,)).fetchone()
    finally:
        conn.close()
    _print(_row_to_dict(row))
    return 0


def cmd_item_update(args: argparse.Namespace) -> int:
    fields, params = [], []
    for col, val in (
        ("name", args.name), ("category", args.category),
        ("interval_days", args.interval_days), ("active", args.active), ("notes", args.notes),
    ):
        if val is not None:
            fields.append(f"{col} = ?")
            params.append(val)
    if not fields:
        print(json.dumps({"error": "no fields to update"}), file=sys.stderr)
        return 2
    fields.append("updated_at = ?")
    params.append(_local_now_iso())
    conn = _connect()
    try:
        params.append(args.id)
        cur = conn.execute(f"UPDATE home_maintenance_items SET {', '.join(fields)} WHERE id = ?", params)
        conn.commit()
        if cur.rowcount == 0:
            print(json.dumps({"error": f"no item with id {args.id}"}), file=sys.stderr)
            return 1
        row = conn.execute("SELECT * FROM home_maintenance_items WHERE id = ?", (args.id,)).fetchone()
    finally:
        conn.close()
    _print(_row_to_dict(row))
    return 0


def cmd_item_list(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        sql = "SELECT * FROM home_maintenance_items WHERE 1=1"
        params: list = []
        if args.category:
            sql += " AND category = ?"
            params.append(args.category)
        if args.active_only:
            sql += " AND active = 1"
        sql += " ORDER BY category, name"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    _print([_row_to_dict(r) for r in rows])
    return 0


def cmd_item_seed(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        existing = conn.execute("SELECT COUNT(*) AS n FROM home_maintenance_items").fetchone()["n"]
        if existing:
            rows = conn.execute("SELECT * FROM home_maintenance_items ORDER BY category, name").fetchall()
            _print({"seeded": False, "reason": "already seeded", "items": [dict(r) for r in rows]})
            return 0
        now = _local_now_iso()
        for item in STARTER_ITEMS:
            conn.execute(
                """
                INSERT INTO home_maintenance_items (
                    id, name, category, interval_days, active, notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (str(uuid.uuid4()), item["name"], item["category"], item["interval_days"], item["notes"], now, now),
            )
        conn.commit()
        rows = conn.execute("SELECT * FROM home_maintenance_items ORDER BY category, name").fetchall()
    finally:
        conn.close()
    _print({"seeded": True, "items": [dict(r) for r in rows]})
    return 0


# ---------------------------------------------------------------------------
# completion
# ---------------------------------------------------------------------------


def cmd_completion_add(args: argparse.Namespace) -> int:
    completed_date = args.completed_date or _today()
    rc = _require_date(completed_date, "--completed-date")
    if rc:
        return rc
    conn = _connect()
    try:
        item = conn.execute("SELECT * FROM home_maintenance_items WHERE id = ?", (args.item_id,)).fetchone()
        if item is None:
            print(json.dumps({"error": f"no item with id {args.item_id}"}), file=sys.stderr)
            return 1
        new_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO home_maintenance_completions (id, item_id, completed_date, notes, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (new_id, args.item_id, completed_date, args.notes, _local_now_iso()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM home_maintenance_completions WHERE id = ?", (new_id,)).fetchone()
    finally:
        conn.close()
    _print(dict(row))
    return 0


def cmd_completion_list(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        sql = "SELECT * FROM home_maintenance_completions WHERE item_id = ? ORDER BY completed_date DESC, created_at DESC"
        params: list = [args.item_id]
        if args.limit is not None:
            sql += " LIMIT ?"
            params.append(args.limit)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    _print([dict(r) for r in rows])
    return 0


def cmd_completion_last(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM home_maintenance_completions WHERE item_id = ? "
            "ORDER BY completed_date DESC, created_at DESC LIMIT 1",
            (args.item_id,),
        ).fetchone()
    finally:
        conn.close()
    _print(dict(row) if row else {})
    return 0


# ---------------------------------------------------------------------------
# due
# ---------------------------------------------------------------------------


def cmd_due_list(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        items = conn.execute("SELECT * FROM home_maintenance_items WHERE active = 1").fetchall()
        result = []
        today = date.fromisoformat(_today())
        for item in items:
            last_row = conn.execute(
                "SELECT completed_date FROM home_maintenance_completions WHERE item_id = ? "
                "ORDER BY completed_date DESC LIMIT 1",
                (item["id"],),
            ).fetchone()
            if last_row is not None:
                last_done = date.fromisoformat(last_row["completed_date"])
            else:
                last_done = date.fromisoformat(item["created_at"][:10])
            next_due = last_done + timedelta(days=item["interval_days"])
            days_until_due = (next_due - today).days
            if days_until_due <= args.horizon_days:
                d = dict(item)
                d["last_done_date"] = last_done.isoformat()
                d["next_due_date"] = next_due.isoformat()
                d["days_until_due"] = days_until_due
                result.append(d)
        result.sort(key=lambda d: d["days_until_due"])
    finally:
        conn.close()
    _print(result)
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Home maintenance store.")
    sub = parser.add_subparsers(dest="command", required=True)

    item = sub.add_parser("item", help="Recurring maintenance item definitions.")
    item_sub = item.add_subparsers(dest="subcommand", required=True)

    ia = item_sub.add_parser("add")
    ia.add_argument("--name", required=True)
    ia.add_argument("--category", required=True, choices=["hvac", "safety", "appliance", "cleaning", "seasonal", "other"])
    ia.add_argument("--interval-days", dest="interval_days", type=int, required=True)
    ia.add_argument("--notes", default=None)
    ia.add_argument("--last-done-date", dest="last_done_date", default=None, help="YYYY-MM-DD; seeds an initial completion row.")
    ia.set_defaults(func=cmd_item_add)

    iu = item_sub.add_parser("update")
    iu.add_argument("--id", required=True)
    iu.add_argument("--name", default=None)
    iu.add_argument("--category", default=None, choices=["hvac", "safety", "appliance", "cleaning", "seasonal", "other"])
    iu.add_argument("--interval-days", dest="interval_days", type=int, default=None)
    iu.add_argument("--active", type=int, default=None, choices=[0, 1])
    iu.add_argument("--notes", default=None)
    iu.set_defaults(func=cmd_item_update)

    il = item_sub.add_parser("list")
    il.add_argument("--category", default=None, choices=["hvac", "safety", "appliance", "cleaning", "seasonal", "other"])
    il.add_argument("--active-only", dest="active_only", action="store_true")
    il.set_defaults(func=cmd_item_list)

    isd = item_sub.add_parser("seed")
    isd.set_defaults(func=cmd_item_seed)

    completion = sub.add_parser("completion", help="Completion log for maintenance items.")
    completion_sub = completion.add_subparsers(dest="subcommand", required=True)

    ca = completion_sub.add_parser("add")
    ca.add_argument("--item-id", dest="item_id", required=True)
    ca.add_argument("--completed-date", dest="completed_date", default=None, help="YYYY-MM-DD; defaults to today.")
    ca.add_argument("--notes", default=None)
    ca.set_defaults(func=cmd_completion_add)

    cl = completion_sub.add_parser("list")
    cl.add_argument("--item-id", dest="item_id", required=True)
    cl.add_argument("--limit", type=int, default=None)
    cl.set_defaults(func=cmd_completion_list)

    clast = completion_sub.add_parser("last")
    clast.add_argument("--item-id", dest="item_id", required=True)
    clast.set_defaults(func=cmd_completion_last)

    due = sub.add_parser("due", help="What's overdue or coming up.")
    due_sub = due.add_subparsers(dest="subcommand", required=True)

    dl = due_sub.add_parser("list")
    dl.add_argument("--horizon-days", dest="horizon_days", type=int, default=7)
    dl.set_defaults(func=cmd_due_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
