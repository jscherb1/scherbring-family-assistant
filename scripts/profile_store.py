#!/usr/bin/env python3
"""Personal profile CLI for the `profile` subagent (and any other subagent that
needs to look up who's who).

Zero-dependency (Python stdlib only), same SQLite file as state_store.py
(state/agent_results.db, schema in state/schema.sql). Single source of truth for
structured facts about the user, family, and friends — name, relationship,
birthday, and open-ended key/value facts (allergy, shirt_size, school, etc.).
Ruth and Claire get rows here too; kids-memory (kid_memories table) separately
owns anecdotes/stories and Drive sync, not structured facts.

Usage:
    python scripts/profile_store.py person add --name "Jane Doe" --relationship spouse \
        [--birthday 1990-03-14] [--notes "..."]
    python scripts/profile_store.py person update --id <id> [--name ...] [--relationship ...] \
        [--birthday ...] [--notes ...]
    python scripts/profile_store.py person get --id <id>
    python scripts/profile_store.py person get --name "Jane Doe"
    python scripts/profile_store.py person list [--search "Jane"]
    python scripts/profile_store.py person search --query "Jane"

    python scripts/profile_store.py fact set --person-id <id> --key allergy --value "peanuts"
    python scripts/profile_store.py fact list --person-id <id>

    python scripts/profile_store.py global-fact set --key home_address --value "123 Main St"
    python scripts/profile_store.py global-fact get --key home_address
    python scripts/profile_store.py global-fact list

All commands print JSON to stdout. `person get` includes the person's facts
under the "facts" key.
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


def _person_with_facts(conn: sqlite3.Connection, person_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM profile_people WHERE id = ?", (person_id,)).fetchone()
    if row is None:
        return None
    facts = conn.execute(
        "SELECT key, value, updated_at FROM profile_people_facts WHERE person_id = ? ORDER BY key",
        (person_id,),
    ).fetchall()
    d = dict(row)
    d["facts"] = [dict(f) for f in facts]
    return d


# ---------------------------------------------------------------------------
# person
# ---------------------------------------------------------------------------


def cmd_person_add(args: argparse.Namespace) -> int:
    now = _local_now_iso()
    new_id = str(uuid.uuid4())
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO profile_people (id, name, relationship, birthday, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (new_id, args.name, args.relationship, args.birthday, args.notes, now, now),
        )
        conn.commit()
        result = _person_with_facts(conn, new_id)
    finally:
        conn.close()
    _print(result)
    return 0


def cmd_person_update(args: argparse.Namespace) -> int:
    fields, params = [], []
    for col, val in (
        ("name", args.name), ("relationship", args.relationship),
        ("birthday", args.birthday), ("notes", args.notes),
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
        cur = conn.execute(f"UPDATE profile_people SET {', '.join(fields)} WHERE id = ?", params)
        conn.commit()
        if cur.rowcount == 0:
            print(json.dumps({"error": f"no person with id {args.id}"}), file=sys.stderr)
            return 1
        result = _person_with_facts(conn, args.id)
    finally:
        conn.close()
    _print(result)
    return 0


def cmd_person_get(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        if args.id:
            result = _person_with_facts(conn, args.id)
        else:
            row = conn.execute(
                "SELECT id FROM profile_people WHERE name = ? COLLATE NOCASE", (args.name,)
            ).fetchone()
            result = _person_with_facts(conn, row["id"]) if row else None
    finally:
        conn.close()
    if result is None:
        print(json.dumps({"error": "person not found"}), file=sys.stderr)
        return 1
    _print(result)
    return 0


def cmd_person_list(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        sql = "SELECT * FROM profile_people WHERE 1=1"
        params: list = []
        if args.search:
            like = f"%{args.search}%"
            sql += " AND (name LIKE ? OR relationship LIKE ? OR notes LIKE ?)"
            params.extend([like, like, like])
        sql += " ORDER BY name"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    _print([dict(r) for r in rows])
    return 0


def cmd_person_search(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        like = f"%{args.query}%"
        rows = conn.execute(
            "SELECT * FROM profile_people WHERE name LIKE ? ORDER BY name", (like,)
        ).fetchall()
    finally:
        conn.close()
    _print([dict(r) for r in rows])
    return 0


# ---------------------------------------------------------------------------
# fact (per-person)
# ---------------------------------------------------------------------------


def cmd_fact_set(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        person = conn.execute("SELECT id FROM profile_people WHERE id = ?", (args.person_id,)).fetchone()
        if person is None:
            print(json.dumps({"error": f"no person with id {args.person_id}"}), file=sys.stderr)
            return 1
        now = _local_now_iso()
        existing = conn.execute(
            "SELECT id FROM profile_people_facts WHERE person_id = ? AND key = ?",
            (args.person_id, args.key),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE profile_people_facts SET value = ?, updated_at = ? WHERE id = ?",
                (args.value, now, existing["id"]),
            )
            fact_id = existing["id"]
        else:
            fact_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO profile_people_facts (id, person_id, key, value, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (fact_id, args.person_id, args.key, args.value, now),
            )
        conn.commit()
        row = conn.execute("SELECT * FROM profile_people_facts WHERE id = ?", (fact_id,)).fetchone()
    finally:
        conn.close()
    _print(dict(row))
    return 0


def cmd_fact_list(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM profile_people_facts WHERE person_id = ? ORDER BY key",
            (args.person_id,),
        ).fetchall()
    finally:
        conn.close()
    _print([dict(r) for r in rows])
    return 0


# ---------------------------------------------------------------------------
# global-fact
# ---------------------------------------------------------------------------


def cmd_global_fact_set(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO profile_facts (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (args.key, args.value, _local_now_iso()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM profile_facts WHERE key = ?", (args.key,)).fetchone()
    finally:
        conn.close()
    _print(dict(row))
    return 0


def cmd_global_fact_get(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM profile_facts WHERE key = ?", (args.key,)).fetchone()
    finally:
        conn.close()
    if row is None:
        print(json.dumps({"error": f"no global fact {args.key}"}), file=sys.stderr)
        return 1
    _print(dict(row))
    return 0


def cmd_global_fact_list(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM profile_facts ORDER BY key").fetchall()
    finally:
        conn.close()
    _print([dict(r) for r in rows])
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Personal profile store.")
    sub = parser.add_subparsers(dest="command", required=True)

    person = sub.add_parser("person", help="People: the user, family, friends.")
    person_sub = person.add_subparsers(dest="subcommand", required=True)

    pa = person_sub.add_parser("add")
    pa.add_argument("--name", required=True)
    pa.add_argument("--relationship", default=None)
    pa.add_argument("--birthday", default=None, help="MM-DD or YYYY-MM-DD")
    pa.add_argument("--notes", default=None)
    pa.set_defaults(func=cmd_person_add)

    pu = person_sub.add_parser("update")
    pu.add_argument("--id", required=True)
    pu.add_argument("--name", default=None)
    pu.add_argument("--relationship", default=None)
    pu.add_argument("--birthday", default=None)
    pu.add_argument("--notes", default=None)
    pu.set_defaults(func=cmd_person_update)

    pg = person_sub.add_parser("get")
    pg_group = pg.add_mutually_exclusive_group(required=True)
    pg_group.add_argument("--id", default=None)
    pg_group.add_argument("--name", default=None)
    pg.set_defaults(func=cmd_person_get)

    pl = person_sub.add_parser("list")
    pl.add_argument("--search", default=None)
    pl.set_defaults(func=cmd_person_list)

    ps = person_sub.add_parser("search")
    ps.add_argument("--query", required=True)
    ps.set_defaults(func=cmd_person_search)

    fact = sub.add_parser("fact", help="Per-person facts (allergy, shirt_size, school, ...).")
    fact_sub = fact.add_subparsers(dest="subcommand", required=True)

    fs = fact_sub.add_parser("set")
    fs.add_argument("--person-id", dest="person_id", required=True)
    fs.add_argument("--key", required=True)
    fs.add_argument("--value", required=True)
    fs.set_defaults(func=cmd_fact_set)

    fl = fact_sub.add_parser("list")
    fl.add_argument("--person-id", dest="person_id", required=True)
    fl.set_defaults(func=cmd_fact_list)

    gfact = sub.add_parser("global-fact", help="Global facts not tied to a person.")
    gfact_sub = gfact.add_subparsers(dest="subcommand", required=True)

    gfs = gfact_sub.add_parser("set")
    gfs.add_argument("--key", required=True)
    gfs.add_argument("--value", required=True)
    gfs.set_defaults(func=cmd_global_fact_set)

    gfg = gfact_sub.add_parser("get")
    gfg.add_argument("--key", required=True)
    gfg.set_defaults(func=cmd_global_fact_get)

    gfl = gfact_sub.add_parser("list")
    gfl.set_defaults(func=cmd_global_fact_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
