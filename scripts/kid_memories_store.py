#!/usr/bin/env python3
"""Kids memory keeper CLI for the kids-memory subagent.

Zero-dependency (Python stdlib only), same SQLite file as state_store.py
(state/agent_results.db, schema in state/schema.sql). The `kids-memory` subagent
calls `add`/`update`/`get`/`list` to manage memories about the kids, and
`mark-drive-synced`/`mark-drive-failed` after attempting the Google Drive upload.
`add-trigger`/`triggers` record and recall which phrasings correctly (or
incorrectly) signaled a kid-specific memory, so recognition improves over time.

Usage:
    python scripts/kid_memories_store.py add --children-json '["Ruth"]' \
        --text "Ruth said the funniest thing at bedtime..." --source telegram \
        [--memory-date 2026-03-14] [--memory-date-precision exact|approximate] \
        [--tags-json '["funny","bedtime"]'] [--metadata-json '{...}']
        (--text is the verbatim raw memory; stored as both raw_text and the initial
        memory_text - raw_text is never touched again by any command. --memory-date is
        when it actually HAPPENED, not when it's being logged; defaults to today if
        omitted. created_at, separately, always records when it was logged.)
    python scripts/kid_memories_store.py update --id <id> \
        [--children-json '["Ruth","Claire"]'] [--memory-text "..."] [--tags-json '[...]'] \
        [--memory-date 2026-03-14] [--memory-date-precision exact|approximate]
        (--memory-text refines/corrects the working copy only; raw_text is immutable)
    python scripts/kid_memories_store.py get --id <id>
    python scripts/kid_memories_store.py list [--child Ruth] [--drive-status pending] [--limit 20]
    python scripts/kid_memories_store.py mark-drive-synced --id <id> --drive-files-json '{...}'
    python scripts/kid_memories_store.py mark-drive-failed --id <id>
    python scripts/kid_memories_store.py add-trigger --phrase "..." --kind positive|false_positive [--note "..."]
    python scripts/kid_memories_store.py triggers [--kind positive]

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
    # Local wall-clock time, not UTC - consistent with scheduler_store.py, since
    # this is what the user means by "today"/"this morning" when dictating a memory.
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for key in ("children_json", "tags_json", "drive_files_json", "metadata_json"):
        if d.get(key):
            d[key] = json.loads(d[key])
    return d


def cmd_add(args: argparse.Namespace) -> int:
    try:
        children = json.loads(args.children_json)
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"invalid --children-json: {exc}"}), file=sys.stderr)
        return 2
    if not isinstance(children, list) or not children:
        print(json.dumps({"error": "--children-json must be a non-empty JSON array"}), file=sys.stderr)
        return 2

    now = _local_now_iso()
    memory_date = args.memory_date or now[:10]
    try:
        datetime.strptime(memory_date, "%Y-%m-%d")
    except ValueError:
        print(json.dumps({"error": f"invalid --memory-date {memory_date!r}, expected YYYY-MM-DD"}), file=sys.stderr)
        return 2

    new_id = str(uuid.uuid4())
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO kid_memories (
                id, created_at, memory_date, memory_date_precision, children_json,
                raw_text, memory_text, source, tags_json, media_type, drive_status, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'text', 'pending', ?)
            """,
            (
                new_id,
                now,
                memory_date,
                args.memory_date_precision,
                json.dumps(children),
                args.text,
                args.text,
                args.source,
                args.tags_json,
                args.metadata_json,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM kid_memories WHERE id = ?", (new_id,)).fetchone()
    finally:
        conn.close()

    print(json.dumps(_row_to_dict(row), ensure_ascii=False, indent=2))
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    fields: list[str] = []
    params: list = []
    if args.children_json is not None:
        try:
            children = json.loads(args.children_json)
        except json.JSONDecodeError as exc:
            print(json.dumps({"error": f"invalid --children-json: {exc}"}), file=sys.stderr)
            return 2
        if not isinstance(children, list) or not children:
            print(json.dumps({"error": "--children-json must be a non-empty JSON array"}), file=sys.stderr)
            return 2
        fields.append("children_json = ?")
        params.append(json.dumps(children))
    if args.memory_text is not None:
        fields.append("memory_text = ?")
        params.append(args.memory_text)
    if args.tags_json is not None:
        fields.append("tags_json = ?")
        params.append(args.tags_json)
    if args.memory_date is not None:
        try:
            datetime.strptime(args.memory_date, "%Y-%m-%d")
        except ValueError:
            print(json.dumps({"error": f"invalid --memory-date {args.memory_date!r}, expected YYYY-MM-DD"}), file=sys.stderr)
            return 2
        fields.append("memory_date = ?")
        params.append(args.memory_date)
    if args.memory_date_precision is not None:
        fields.append("memory_date_precision = ?")
        params.append(args.memory_date_precision)
    if not fields:
        print(json.dumps({"error": "no fields to update"}), file=sys.stderr)
        return 2

    conn = _connect()
    try:
        params.append(args.id)
        cur = conn.execute(f"UPDATE kid_memories SET {', '.join(fields)} WHERE id = ?", params)
        conn.commit()
        if cur.rowcount == 0:
            print(json.dumps({"error": f"no kid memory with id {args.id}"}), file=sys.stderr)
            return 1
        row = conn.execute("SELECT * FROM kid_memories WHERE id = ?", (args.id,)).fetchone()
    finally:
        conn.close()
    print(json.dumps(_row_to_dict(row), ensure_ascii=False, indent=2))
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM kid_memories WHERE id = ?", (args.id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        print(json.dumps({"error": f"no kid memory with id {args.id}"}), file=sys.stderr)
        return 1
    print(json.dumps(_row_to_dict(row), ensure_ascii=False, indent=2))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        sql = "SELECT * FROM kid_memories WHERE 1=1"
        params: list = []
        if args.child:
            sql += " AND children_json LIKE ?"
            params.append(f'%"{args.child}"%')
        if args.drive_status:
            sql += " AND drive_status = ?"
            params.append(args.drive_status)
        if args.memory_date:
            sql += " AND memory_date = ?"
            params.append(args.memory_date)
        sql += " ORDER BY memory_date DESC, created_at DESC"
        if args.limit is not None:
            sql += " LIMIT ?"
            params.append(args.limit)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    print(json.dumps([_row_to_dict(r) for r in rows], ensure_ascii=False, indent=2))
    return 0


def cmd_mark_drive_synced(args: argparse.Namespace) -> int:
    try:
        json.loads(args.drive_files_json)
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"invalid --drive-files-json: {exc}"}), file=sys.stderr)
        return 2
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE kid_memories SET drive_status = 'synced', drive_files_json = ? WHERE id = ?",
            (args.drive_files_json, args.id),
        )
        conn.commit()
        if cur.rowcount == 0:
            print(json.dumps({"error": f"no kid memory with id {args.id}"}), file=sys.stderr)
            return 1
        row = conn.execute("SELECT * FROM kid_memories WHERE id = ?", (args.id,)).fetchone()
    finally:
        conn.close()
    print(json.dumps(_row_to_dict(row), ensure_ascii=False, indent=2))
    return 0


def cmd_mark_drive_failed(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        cur = conn.execute("UPDATE kid_memories SET drive_status = 'failed' WHERE id = ?", (args.id,))
        conn.commit()
        if cur.rowcount == 0:
            print(json.dumps({"error": f"no kid memory with id {args.id}"}), file=sys.stderr)
            return 1
        row = conn.execute("SELECT * FROM kid_memories WHERE id = ?", (args.id,)).fetchone()
    finally:
        conn.close()
    print(json.dumps(_row_to_dict(row), ensure_ascii=False, indent=2))
    return 0


def cmd_add_trigger(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO kid_memory_triggers (phrase, kind, created_at, note) VALUES (?, ?, ?, ?)",
            (args.phrase, args.kind, _local_now_iso(), args.note),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM kid_memory_triggers WHERE id = ?", (cur.lastrowid,)).fetchone()
    finally:
        conn.close()
    print(json.dumps(dict(row), ensure_ascii=False, indent=2))
    return 0


def cmd_triggers(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        sql = "SELECT * FROM kid_memory_triggers WHERE 1=1"
        params: list = []
        if args.kind:
            sql += " AND kind = ?"
            params.append(args.kind)
        sql += " ORDER BY created_at DESC"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kids memory keeper store.")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="Save a new kid memory.")
    add.add_argument("--children-json", dest="children_json", required=True)
    add.add_argument("--text", required=True)
    add.add_argument("--source", required=True, choices=["telegram", "direct"])
    add.add_argument("--memory-date", dest="memory_date", default=None, help="YYYY-MM-DD when it actually happened; defaults to today.")
    add.add_argument("--memory-date-precision", dest="memory_date_precision", default="exact", choices=["exact", "approximate"])
    add.add_argument("--tags-json", dest="tags_json", default=None)
    add.add_argument("--metadata-json", dest="metadata_json", default=None)
    add.set_defaults(func=cmd_add)

    upd = sub.add_parser("update", help="Correct a memory's children/text/tags/date.")
    upd.add_argument("--id", required=True)
    upd.add_argument("--children-json", dest="children_json", default=None)
    upd.add_argument("--memory-text", dest="memory_text", default=None)
    upd.add_argument("--tags-json", dest="tags_json", default=None)
    upd.add_argument("--memory-date", dest="memory_date", default=None)
    upd.add_argument("--memory-date-precision", dest="memory_date_precision", default=None, choices=["exact", "approximate"])
    upd.set_defaults(func=cmd_update)

    get = sub.add_parser("get", help="Get one kid memory by --id.")
    get.add_argument("--id", required=True)
    get.set_defaults(func=cmd_get)

    ls = sub.add_parser("list", help="List kid memories.")
    ls.add_argument("--child", default=None)
    ls.add_argument("--drive-status", dest="drive_status", default=None, choices=["pending", "synced", "failed"])
    ls.add_argument("--memory-date", dest="memory_date", default=None, help="Exact YYYY-MM-DD filter.")
    ls.add_argument("--limit", type=int, default=None)
    ls.set_defaults(func=cmd_list)

    mds = sub.add_parser("mark-drive-synced", help="Record successful Drive upload.")
    mds.add_argument("--id", required=True)
    mds.add_argument("--drive-files-json", dest="drive_files_json", required=True)
    mds.set_defaults(func=cmd_mark_drive_synced)

    mdf = sub.add_parser("mark-drive-failed", help="Record a failed Drive upload attempt.")
    mdf.add_argument("--id", required=True)
    mdf.set_defaults(func=cmd_mark_drive_failed)

    at = sub.add_parser("add-trigger", help="Record a learned trigger phrase.")
    at.add_argument("--phrase", required=True)
    at.add_argument("--kind", required=True, choices=["positive", "false_positive"])
    at.add_argument("--note", default=None)
    at.set_defaults(func=cmd_add_trigger)

    trg = sub.add_parser("triggers", help="List learned trigger phrases.")
    trg.add_argument("--kind", default=None, choices=["positive", "false_positive"])
    trg.set_defaults(func=cmd_triggers)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
