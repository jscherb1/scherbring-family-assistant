#!/usr/bin/env python3
"""Recipe library CLI for the meal-planner subagent.

Zero-dependency (Python stdlib only), same SQLite file as state_store.py
(state/agent_results.db, schema in state/schema.sql). Subagents call this via the
Bash tool to look up recipe candidates, save newly-generated meals back into the
library, and record cooking history / user feedback.

Usage:
    python scripts/recipes_store.py import --file recipes.json
    python scripts/recipes_store.py list [--meal-type dinner] [--protein-type chicken]
        [--tag Crockpot] [--search "chicken"] [--exclude-cooked-since 2026-07-06]
        [--min-rating 3] [--limit 20]
    python scripts/recipes_store.py get --id <id>
    python scripts/recipes_store.py add --title "..." --ingredients-json '[...]'
        --steps-json '[...]' [--description ...] [--tags-json '[...]']
        [--protein-type chicken] [--meal-type dinner] [--prep-time-min 20]
        [--cook-time-min 30] [--servings 4] [--source-url ...]
    python scripts/recipes_store.py mark-cooked --id <id> --date 2026-07-20
    python scripts/recipes_store.py feedback --id <id> --comment "too dry" [--rating 2]

All commands print JSON to stdout.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "state" / "agent_results.db"
SCHEMA_PATH = REPO_ROOT / "state" / "schema.sql"

# Recipe text contains non-ASCII characters (½, —, etc.); force UTF-8 on stdout/stderr
# so this never crashes on Windows' default console codepage, regardless of caller env.
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
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for key in ("ingredients_json", "steps_json", "tags_json", "feedback_json"):
        if d.get(key):
            d[key] = json.loads(d[key])
    return d


def cmd_import(args: argparse.Namespace) -> int:
    path = Path(args.file)
    data = json.loads(path.read_text(encoding="utf-8"))
    recipes = data.get("recipes", data if isinstance(data, list) else [])

    conn = _connect()
    inserted = 0
    try:
        for r in recipes:
            conn.execute(
                """
                INSERT OR REPLACE INTO recipes (
                    id, title, description, ingredients_json, steps_json, tags_json,
                    protein_type, meal_type, prep_time_min, cook_time_min,
                    total_time_min, servings, source_url, notes, last_cooked_at,
                    rating, feedback_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r.get("id") or str(uuid.uuid4()),
                    r["title"],
                    r.get("description"),
                    json.dumps(r.get("ingredients", []), ensure_ascii=False),
                    json.dumps(r.get("steps", []), ensure_ascii=False),
                    json.dumps(r.get("tags", []), ensure_ascii=False),
                    r.get("proteinType"),
                    r.get("mealType"),
                    r.get("prepTimeMin"),
                    r.get("cookTimeMin"),
                    r.get("totalTimeMin"),
                    r.get("servings"),
                    r.get("sourceUrl"),
                    r.get("notes"),
                    r.get("lastCookedAt"),
                    None,
                    None,
                ),
            )
            inserted += 1
        conn.commit()
    finally:
        conn.close()

    print(json.dumps({"imported": inserted}))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        sql = "SELECT * FROM recipes WHERE 1=1"
        params: list = []
        if args.meal_type:
            sql += " AND meal_type = ?"
            params.append(args.meal_type)
        if args.protein_type:
            sql += " AND protein_type = ?"
            params.append(args.protein_type)
        if args.tag:
            sql += " AND tags_json LIKE ?"
            params.append(f'%"{args.tag}"%')
        if args.search:
            sql += " AND (title LIKE ? OR description LIKE ?)"
            like = f"%{args.search}%"
            params.extend([like, like])
        if args.exclude_cooked_since:
            sql += " AND (last_cooked_at IS NULL OR last_cooked_at < ?)"
            params.append(args.exclude_cooked_since)
        if args.min_rating is not None:
            sql += " AND (rating IS NULL OR rating >= ?)"
            params.append(args.min_rating)
        sql += " ORDER BY title"
        if args.limit is not None:
            sql += " LIMIT ?"
            params.append(args.limit)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    print(json.dumps([_row_to_dict(r) for r in rows], ensure_ascii=False, indent=2))
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM recipes WHERE id = ?", (args.id,)).fetchone()
    finally:
        conn.close()

    if row is None:
        print(json.dumps({"error": f"no recipe with id {args.id}"}), file=sys.stderr)
        return 1

    print(json.dumps(_row_to_dict(row), ensure_ascii=False, indent=2))
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    for name, value in (("ingredients-json", args.ingredients_json), ("steps-json", args.steps_json)):
        try:
            json.loads(value)
        except json.JSONDecodeError as exc:
            print(f"error: --{name} is not valid JSON: {exc}", file=sys.stderr)
            return 2
    if args.tags_json is not None:
        try:
            json.loads(args.tags_json)
        except json.JSONDecodeError as exc:
            print(f"error: --tags-json is not valid JSON: {exc}", file=sys.stderr)
            return 2

    new_id = str(uuid.uuid4())
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO recipes (
                id, title, description, ingredients_json, steps_json, tags_json,
                protein_type, meal_type, prep_time_min, cook_time_min, servings,
                source_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id,
                args.title,
                args.description,
                args.ingredients_json,
                args.steps_json,
                args.tags_json,
                args.protein_type,
                args.meal_type,
                args.prep_time_min,
                args.cook_time_min,
                args.servings,
                args.source_url,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM recipes WHERE id = ?", (new_id,)).fetchone()
    finally:
        conn.close()

    print(json.dumps(_row_to_dict(row), ensure_ascii=False, indent=2))
    return 0


def cmd_mark_cooked(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE recipes SET last_cooked_at = ? WHERE id = ?", (args.date, args.id)
        )
        conn.commit()
        if cur.rowcount == 0:
            print(json.dumps({"error": f"no recipe with id {args.id}"}), file=sys.stderr)
            return 1
        row = conn.execute("SELECT * FROM recipes WHERE id = ?", (args.id,)).fetchone()
    finally:
        conn.close()

    print(json.dumps(_row_to_dict(row), ensure_ascii=False, indent=2))
    return 0


def cmd_feedback(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM recipes WHERE id = ?", (args.id,)).fetchone()
        if row is None:
            print(json.dumps({"error": f"no recipe with id {args.id}"}), file=sys.stderr)
            return 1

        feedback = json.loads(row["feedback_json"]) if row["feedback_json"] else []
        feedback.append(
            {
                "date": _utc_now_iso()[:10],
                "comment": args.comment,
                "rating": args.rating,
            }
        )
        new_rating = args.rating if args.rating is not None else row["rating"]

        conn.execute(
            "UPDATE recipes SET feedback_json = ?, rating = ? WHERE id = ?",
            (json.dumps(feedback, ensure_ascii=False), new_rating, args.id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM recipes WHERE id = ?", (args.id,)).fetchone()
    finally:
        conn.close()

    print(json.dumps(_row_to_dict(row), ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Meal-planner recipe library.")
    sub = parser.add_subparsers(dest="command", required=True)

    imp = sub.add_parser("import", help="One-time migration from recipes.json.")
    imp.add_argument("--file", required=True)
    imp.set_defaults(func=cmd_import)

    ls = sub.add_parser("list", help="List/filter recipe candidates.")
    ls.add_argument("--meal-type", dest="meal_type", default=None)
    ls.add_argument("--protein-type", dest="protein_type", default=None)
    ls.add_argument("--tag", default=None)
    ls.add_argument("--search", default=None)
    ls.add_argument("--exclude-cooked-since", dest="exclude_cooked_since", default=None)
    ls.add_argument("--min-rating", dest="min_rating", type=int, default=None)
    ls.add_argument("--limit", type=int, default=None)
    ls.set_defaults(func=cmd_list)

    get = sub.add_parser("get", help="Full detail for one recipe.")
    get.add_argument("--id", required=True)
    get.set_defaults(func=cmd_get)

    add = sub.add_parser("add", help="Save a newly-generated meal into the library.")
    add.add_argument("--title", required=True)
    add.add_argument("--description", default=None)
    add.add_argument("--ingredients-json", dest="ingredients_json", required=True)
    add.add_argument("--steps-json", dest="steps_json", required=True)
    add.add_argument("--tags-json", dest="tags_json", default=None)
    add.add_argument("--protein-type", dest="protein_type", default=None)
    add.add_argument("--meal-type", dest="meal_type", default="dinner")
    add.add_argument("--prep-time-min", dest="prep_time_min", type=int, default=None)
    add.add_argument("--cook-time-min", dest="cook_time_min", type=int, default=None)
    add.add_argument("--servings", type=int, default=None)
    add.add_argument("--source-url", dest="source_url", default=None)
    add.set_defaults(func=cmd_add)

    mc = sub.add_parser("mark-cooked", help="Record when a recipe was planned/cooked.")
    mc.add_argument("--id", required=True)
    mc.add_argument("--date", required=True)
    mc.set_defaults(func=cmd_mark_cooked)

    fb = sub.add_parser("feedback", help="Record user feedback on a recipe.")
    fb.add_argument("--id", required=True)
    fb.add_argument("--comment", required=True)
    fb.add_argument("--rating", type=int, default=None)
    fb.set_defaults(func=cmd_feedback)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
