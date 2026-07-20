#!/usr/bin/env python3
"""Scheduled-tasks registry CLI for the scheduler subagent and poller.

Zero-dependency (Python stdlib only), same SQLite file as state_store.py
(state/agent_results.db, schema in state/schema.sql). The `scheduler` subagent calls
`add`/`list`/`get`/`enable`/`disable`/`delete` to manage tasks conversationally.
scripts/scheduler_poll.py calls `due` to find what to fire and `mark-dispatched` to
record it. The orchestrator calls `log-run` after completing a fired task's work, and
the heartbeat task calls `runs` to look for anything overdue/failed.

Cron matching is a hand-rolled 5-field matcher (minute hour day-of-month month
day-of-week) supporting wildcards, single values, lists, ranges, and steps - the same
subset Claude Code's own cron scheduler supports. No third-party dependency.

Usage:
    python scripts/scheduler_store.py add --name "weekly-meal-plan" \
        --prompt "Delegate to the meal-planner subagent to plan next week's dinners, ..." \
        --cron "0 9 * * 0" --target-chat-id "482910..."
    python scripts/scheduler_store.py list [--enabled-only]
    python scripts/scheduler_store.py get --name "weekly-meal-plan"
    python scripts/scheduler_store.py enable --name "weekly-meal-plan"
    python scripts/scheduler_store.py disable --name "weekly-meal-plan"
    python scripts/scheduler_store.py delete --name "weekly-meal-plan"
    python scripts/scheduler_store.py due [--now <iso8601>]
    python scripts/scheduler_store.py mark-dispatched --id <id> --run-at <iso8601>
    python scripts/scheduler_store.py log-run --task-id <id> --run-at <iso8601> \
        --status dispatched|dispatch_failed|ok|failed [--summary "..."] [--dispatched-at <iso8601>]
    python scripts/scheduler_store.py runs [--task-id <id>] [--since <iso8601>] \
        [--status ok,failed] [--limit 20]

All commands print JSON to stdout.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "state" / "agent_results.db"
SCHEMA_PATH = REPO_ROOT / "state" / "schema.sql"

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def _utc_now_iso() -> str:
    # Despite the name (kept for call-site compatibility), this returns the machine's
    # local wall-clock time, not UTC. Cron expressions are written by users/agents in
    # local time ("13:17" means 1:17 PM here), and the Windows Scheduled Task that
    # drives the poller also fires on local time - so every timestamp this module
    # stores or compares must live in that same local wall-clock frame, or "due"
    # matching silently drifts by the system's UTC offset.
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _parse_iso(value: str) -> datetime:
    # Naive local wall-clock timestamps only - see _utc_now_iso.
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1]
    return datetime.fromisoformat(v).replace(tzinfo=None)


def _fmt_iso(dt: datetime) -> str:
    return dt.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")


# --------------------------------------------------------------------------------
# Hand-rolled 5-field cron matcher: minute hour day-of-month month day-of-week.
# Supports: '*', single values, comma lists, ranges ('a-b'), steps ('*/n', 'a-b/n').
# day-of-week: 0-6 (Sunday=0), also accepts 7 for Sunday (folded to 0).
# When both day-of-month and day-of-week are restricted (not '*'), a date matches if
# EITHER field matches (standard vixie-cron semantics).
# --------------------------------------------------------------------------------

_FIELD_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]


class CronError(ValueError):
    pass


def _parse_field(field: str, lo: int, hi: int) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"empty field component in {field!r}")
        step = 1
        if "/" in part:
            base, step_str = part.split("/", 1)
            try:
                step = int(step_str)
            except ValueError:
                raise CronError(f"bad step in {part!r}") from None
            if step <= 0:
                raise CronError(f"step must be positive in {part!r}")
        else:
            base = part

        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, b = base.split("-", 1)
            try:
                start, end = int(a), int(b)
            except ValueError:
                raise CronError(f"bad range in {part!r}") from None
        else:
            try:
                start = end = int(base)
            except ValueError:
                raise CronError(f"bad value in {part!r}") from None

        if not (lo <= start <= hi and lo <= end <= hi and start <= end):
            raise CronError(f"value out of range [{lo},{hi}] in {part!r}")

        values.update(range(start, end + 1, step))
    return values


def parse_cron(expr: str):
    fields = expr.strip().split()
    if len(fields) != 5:
        raise CronError(f"expected 5 fields (minute hour dom month dow), got {len(fields)}: {expr!r}")
    minute, hour, dom, month, dow = (
        _parse_field(f, lo, hi) for f, (lo, hi) in zip(fields, _FIELD_RANGES)
    )
    # Fold Sunday=7 into Sunday=0.
    dow = {(d % 7) for d in dow}
    dom_wild = fields[2].strip() == "*"
    dow_wild = fields[4].strip() == "*"
    return minute, hour, dom, month, dow, dom_wild, dow_wild


def _matches(dt: datetime, parsed) -> bool:
    minute, hour, dom, month, dow, dom_wild, dow_wild = parsed
    if dt.minute not in minute or dt.hour not in hour or dt.month not in month:
        return False
    dom_ok = dt.day in dom
    dow_ok = (dt.isoweekday() % 7) in dow  # Python Monday=1..Sunday=7 -> Sunday=0
    if dom_wild and dow_wild:
        return True
    if dom_wild:
        return dow_ok
    if dow_wild:
        return dom_ok
    return dom_ok or dow_ok


def iter_matches_after(cron_expression: str, after: datetime, limit: int = 100000):
    """Yield matching minute-aligned datetimes strictly after `after`, in order."""
    parsed = parse_cron(cron_expression)
    cur = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    for _ in range(limit):
        if _matches(cur, parsed):
            yield cur
        cur += timedelta(minutes=1)


def due_occurrences(cron_expression: str, base: datetime, now: datetime, cap: int = 5 * 365 * 24 * 60):
    """Return the single most-recent fully-elapsed occurrence after `base` and at or
    before `now`, or None if none is due yet. Deliberately collapses any burst of
    missed occurrences (e.g. after a multi-day outage) down to just the latest one -
    no catch-up storm.
    """
    latest = None
    for occ in iter_matches_after(cron_expression, base, limit=cap):
        if occ > now:
            break
        latest = occ
    return latest


# --------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------


def cmd_add(args: argparse.Namespace) -> int:
    try:
        parse_cron(args.cron)
    except CronError as exc:
        print(json.dumps({"error": f"invalid --cron: {exc}"}), file=sys.stderr)
        return 2

    new_id = str(uuid.uuid4())
    conn = _connect()
    try:
        try:
            conn.execute(
                """
                INSERT INTO scheduled_tasks (
                    id, name, prompt, cron_expression, target_chat_id, enabled, created_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (new_id, args.name, args.prompt, args.cron, args.target_chat_id, _utc_now_iso()),
            )
        except sqlite3.IntegrityError:
            print(json.dumps({"error": f"a scheduled task named {args.name!r} already exists"}), file=sys.stderr)
            return 1
        conn.commit()
        row = conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (new_id,)).fetchone()
    finally:
        conn.close()

    print(json.dumps(dict(row), ensure_ascii=False, indent=2))
    return 0


def _find_task(conn: sqlite3.Connection, args: argparse.Namespace) -> sqlite3.Row | None:
    if getattr(args, "id", None):
        return conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (args.id,)).fetchone()
    if getattr(args, "name", None):
        return conn.execute("SELECT * FROM scheduled_tasks WHERE name = ?", (args.name,)).fetchone()
    return None


def cmd_list(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        sql = "SELECT * FROM scheduled_tasks"
        params: list = []
        if args.enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY name"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        row = _find_task(conn, args)
    finally:
        conn.close()
    if row is None:
        print(json.dumps({"error": "no matching scheduled task"}), file=sys.stderr)
        return 1
    print(json.dumps(dict(row), ensure_ascii=False, indent=2))
    return 0


def _set_enabled(args: argparse.Namespace, enabled: int) -> int:
    conn = _connect()
    try:
        row = _find_task(conn, args)
        if row is None:
            print(json.dumps({"error": "no matching scheduled task"}), file=sys.stderr)
            return 1
        conn.execute("UPDATE scheduled_tasks SET enabled = ? WHERE id = ?", (enabled, row["id"]))
        conn.commit()
        row = conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (row["id"],)).fetchone()
    finally:
        conn.close()
    print(json.dumps(dict(row), ensure_ascii=False, indent=2))
    return 0


def cmd_enable(args: argparse.Namespace) -> int:
    return _set_enabled(args, 1)


def cmd_disable(args: argparse.Namespace) -> int:
    return _set_enabled(args, 0)


def cmd_delete(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        row = _find_task(conn, args)
        if row is None:
            print(json.dumps({"error": "no matching scheduled task"}), file=sys.stderr)
            return 1
        conn.execute("DELETE FROM scheduled_task_runs WHERE task_id = ?", (row["id"],))
        conn.execute("DELETE FROM scheduled_tasks WHERE id = ?", (row["id"],))
        conn.commit()
    finally:
        conn.close()
    print(json.dumps({"deleted": row["id"], "name": row["name"]}))
    return 0


def cmd_due(args: argparse.Namespace) -> int:
    now = _parse_iso(args.now) if args.now else datetime.now()
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM scheduled_tasks WHERE enabled = 1").fetchall()
    finally:
        conn.close()

    out = []
    for row in rows:
        base = _parse_iso(row["last_dispatched_at"] or row["created_at"])
        try:
            occ = due_occurrences(row["cron_expression"], base, now)
        except CronError as exc:
            print(f"warning: skipping {row['name']!r}, bad cron: {exc}", file=sys.stderr)
            continue
        if occ is not None:
            d = dict(row)
            d["due_run_at"] = _fmt_iso(occ)
            out.append(d)

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_mark_dispatched(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE scheduled_tasks SET last_dispatched_at = ? WHERE id = ?",
            (args.run_at, args.id),
        )
        conn.commit()
        if cur.rowcount == 0:
            print(json.dumps({"error": f"no scheduled task with id {args.id}"}), file=sys.stderr)
            return 1
        row = conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (args.id,)).fetchone()
    finally:
        conn.close()
    print(json.dumps(dict(row), ensure_ascii=False, indent=2))
    return 0


def cmd_log_run(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            """
            INSERT INTO scheduled_task_runs (task_id, run_at, dispatched_at, status, summary)
            VALUES (?, ?, ?, ?, ?)
            """,
            (args.task_id, args.run_at or _utc_now_iso(), args.dispatched_at, args.status, args.summary),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM scheduled_task_runs WHERE id = ?", (cur.lastrowid,)).fetchone()
    finally:
        conn.close()
    print(json.dumps(dict(row), ensure_ascii=False, indent=2))
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        sql = "SELECT * FROM scheduled_task_runs WHERE 1=1"
        params: list = []
        if args.task_id:
            sql += " AND task_id = ?"
            params.append(args.task_id)
        if args.since:
            sql += " AND run_at >= ?"
            params.append(args.since)
        if args.status:
            statuses = [s.strip() for s in args.status.split(",") if s.strip()]
            sql += " AND status IN ({})".format(",".join("?" for _ in statuses))
            params.extend(statuses)
        sql += " ORDER BY run_at DESC"
        if args.limit is not None:
            sql += " LIMIT ?"
            params.append(args.limit)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scheduled-tasks registry.")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="Create a new scheduled task.")
    add.add_argument("--name", required=True)
    add.add_argument("--prompt", required=True)
    add.add_argument("--cron", required=True)
    add.add_argument("--target-chat-id", dest="target_chat_id", required=True)
    add.set_defaults(func=cmd_add)

    ls = sub.add_parser("list", help="List scheduled tasks.")
    ls.add_argument("--enabled-only", action="store_true")
    ls.set_defaults(func=cmd_list)

    get = sub.add_parser("get", help="Get one scheduled task by --id or --name.")
    get.add_argument("--id", default=None)
    get.add_argument("--name", default=None)
    get.set_defaults(func=cmd_get)

    en = sub.add_parser("enable", help="Enable a scheduled task by --id or --name.")
    en.add_argument("--id", default=None)
    en.add_argument("--name", default=None)
    en.set_defaults(func=cmd_enable)

    dis = sub.add_parser("disable", help="Disable (pause) a scheduled task by --id or --name.")
    dis.add_argument("--id", default=None)
    dis.add_argument("--name", default=None)
    dis.set_defaults(func=cmd_disable)

    dele = sub.add_parser("delete", help="Delete a scheduled task and its run history.")
    dele.add_argument("--id", default=None)
    dele.add_argument("--name", default=None)
    dele.set_defaults(func=cmd_delete)

    due = sub.add_parser("due", help="List enabled tasks that have a due, undispatched occurrence.")
    due.add_argument("--now", default=None, help="ISO-8601 override for testing; defaults to current UTC time.")
    due.set_defaults(func=cmd_due)

    md = sub.add_parser("mark-dispatched", help="Advance a task's last_dispatched_at.")
    md.add_argument("--id", required=True)
    md.add_argument("--run-at", dest="run_at", required=True)
    md.set_defaults(func=cmd_mark_dispatched)

    lr = sub.add_parser("log-run", help="Record a run's status (dispatched/dispatch_failed/ok/failed).")
    lr.add_argument("--task-id", dest="task_id", required=True)
    lr.add_argument("--run-at", dest="run_at", default=None)
    lr.add_argument("--dispatched-at", dest="dispatched_at", default=None)
    lr.add_argument("--status", required=True, choices=["dispatched", "dispatch_failed", "ok", "failed"])
    lr.add_argument("--summary", default=None)
    lr.set_defaults(func=cmd_log_run)

    runs = sub.add_parser("runs", help="Query run history.")
    runs.add_argument("--task-id", dest="task_id", default=None)
    runs.add_argument("--since", default=None, help="ISO-8601; only runs at/after this time.")
    runs.add_argument("--status", default=None, help="Comma-separated list, e.g. failed,dispatch_failed")
    runs.add_argument("--limit", type=int, default=None)
    runs.set_defaults(func=cmd_runs)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
