#!/usr/bin/env python3
"""Fitness planning CLI for the fitness subagent.

Zero-dependency (Python stdlib only), same SQLite file as state_store.py
(state/agent_results.db, schema in state/schema.sql). Holds the local cache
of the COROS Training Hub Workout Library (fed by
scripts/fitness/library_sync.py) and, later, the weekly plan log.

Usage:
    python scripts/fitness_store.py workout sync
        Reads a JSON array of {coros_workout_id, name, workout_type,
        sets_desc, target_distance, target_time, estimated_load} from
        stdin (the exact shape scripts/fitness/library_sync.py prints) and
        treats it as the FULL current Coros library: upserts every item,
        keyed on coros_workout_id, and deletes any existing
        fitness_workout_library row whose coros_workout_id is absent from
        the input (it was removed/renamed in Coros since the last sync).
        Prints {"added": N, "updated": N, "removed": N, "workouts": [...]}.

    python scripts/fitness_store.py workout list [--type outrun]
        [--search "peloton"]

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


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def _local_now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# workout
# ---------------------------------------------------------------------------


def cmd_workout_sync(args: argparse.Namespace) -> int:
    raw = sys.stdin.read()
    try:
        items = json.loads(raw) if raw.strip() else []
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"invalid JSON on stdin: {exc}"}), file=sys.stderr)
        return 2

    now = _local_now_iso()
    conn = _connect()
    synced = []
    added = 0
    updated = 0
    try:
        for item in items:
            coros_id = item["coros_workout_id"]
            existing = conn.execute(
                "SELECT id FROM fitness_workout_library WHERE coros_workout_id = ?",
                (coros_id,),
            ).fetchone()
            if existing:
                updated += 1
            else:
                added += 1
            row_id = existing["id"] if existing else str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO fitness_workout_library
                    (id, coros_workout_id, name, workout_type, sets_desc,
                     target_distance, target_time, estimated_load,
                     last_synced_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(coros_workout_id) DO UPDATE SET
                    name=excluded.name,
                    workout_type=excluded.workout_type,
                    sets_desc=excluded.sets_desc,
                    target_distance=excluded.target_distance,
                    target_time=excluded.target_time,
                    estimated_load=excluded.estimated_load,
                    last_synced_at=excluded.last_synced_at
                """,
                (
                    row_id,
                    coros_id,
                    item["name"],
                    item["workout_type"],
                    item.get("sets_desc"),
                    item.get("target_distance"),
                    item.get("target_time"),
                    item.get("estimated_load"),
                    now,
                    now,
                ),
            )
            synced.append(coros_id)

        removed = 0
        existing_ids = [
            r["coros_workout_id"]
            for r in conn.execute("SELECT coros_workout_id FROM fitness_workout_library").fetchall()
        ]
        stale_ids = [cid for cid in existing_ids if cid not in synced]
        if stale_ids:
            conn.execute(
                "DELETE FROM fitness_workout_library WHERE coros_workout_id IN ({})".format(
                    ",".join("?" * len(stale_ids))
                ),
                stale_ids,
            )
            removed = len(stale_ids)

        conn.commit()
        rows = conn.execute("SELECT * FROM fitness_workout_library ORDER BY name").fetchall()
    finally:
        conn.close()
    _print(
        {
            "added": added,
            "updated": updated,
            "removed": removed,
            "workouts": [dict(r) for r in rows],
        }
    )
    return 0


def cmd_workout_list(args: argparse.Namespace) -> int:
    query = "SELECT * FROM fitness_workout_library WHERE 1=1"
    params: list = []
    if args.type:
        query += " AND workout_type = ?"
        params.append(args.type)
    if args.search:
        query += " AND name LIKE ?"
        params.append(f"%{args.search}%")
    query += " ORDER BY name"

    conn = _connect()
    try:
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    _print([dict(r) for r in rows])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fitness planning store.")
    sub = parser.add_subparsers(dest="command", required=True)

    workout = sub.add_parser("workout", help="Coros Workout Library cache.")
    workout_sub = workout.add_subparsers(dest="subcommand", required=True)

    ws = workout_sub.add_parser("sync", help="upsert workouts from a JSON array on stdin")
    ws.set_defaults(func=cmd_workout_sync)

    wl = workout_sub.add_parser("list")
    wl.add_argument("--type", default=None, help="filter by workout_type (e.g. outrun, strength, cycle)")
    wl.add_argument("--search", default=None, help="substring match on name")
    wl.set_defaults(func=cmd_workout_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
