#!/usr/bin/env python3
"""Recipe library CLI for the meal-planner subagent.

Zero-dependency (Python stdlib only), same SQLite file as state_store.py
(state/agent_results.db, schema in state/schema.sql). A thin wrapper over the shared
service in scripts/recipes.py, which the dashboard also uses. Subagents call this via the
Bash tool to look up recipe candidates, save newly-generated meals back into the
library, edit or delete recipes, and record cooking history / user feedback.

Usage:
    python scripts/recipes_store.py import --file recipes.json
    python scripts/recipes_store.py list [--meal-type dinner] [--protein-type chicken]
        [--tag Crockpot] [--search "chicken"] [--exclude-cooked-since 2026-07-06]
        [--min-rating 3] [--limit 20]
    python scripts/recipes_store.py get --id <id>
    python scripts/recipes_store.py add --title "..." --ingredients-json '[...]'
        --steps-json '[...]' [--description ...] [--tags-json '[...]']
        [--protein-type chicken] [--meal-type dinner] [--prep-time-min 20]
        [--cook-time-min 30] [--servings 4] [--source-url ...] [--notes ...]
    python scripts/recipes_store.py update --id <id> [--title ...] [--description ...]
        [--ingredients-json '[...]'] [--steps-json '[...]'] [--tags-json '[...]']
        [--protein-type ...] [--meal-type ...] [--prep-time-min N] [--cook-time-min N]
        [--total-time-min N] [--servings N] [--source-url ...] [--notes ...]
        [--clear description source_url ...]   (clear optional fields)
    python scripts/recipes_store.py delete --id <id>      (permanent; confirm with the user first)
    python scripts/recipes_store.py mark-cooked --id <id> --date 2026-07-20
    python scripts/recipes_store.py feedback --id <id> --comment "too dry" [--rating 2]

All commands print JSON to stdout. Errors print {"error": ...} to stderr and exit 1 (not found)
or 2 (bad input).
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import recipes as service  # noqa: E402

DB_PATH = service.DB_PATH

# Recipe text contains non-ASCII characters (½, —, etc.); force UTF-8 on stdout/stderr
# so this never crashes on Windows' default console codepage, regardless of caller env.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def _connect():
    return service.connect(DB_PATH)


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


def _emit(recipe: dict) -> int:
    print(json.dumps(recipe, ensure_ascii=False, indent=2))
    return 0


def _json_arg(value: str | None, name: str):
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise service.RecipeError(f"--{name} is not valid JSON: {exc}") from None


def cmd_list(args: argparse.Namespace) -> int:
    items = service.list_recipes(
        meal_type=args.meal_type, protein_type=args.protein_type, tag=args.tag, search=args.search,
        exclude_cooked_since=args.exclude_cooked_since, min_rating=args.min_rating, limit=args.limit,
        db_path=DB_PATH)
    if args.compact:
        print(json.dumps([service.compact(i) for i in items], ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(items, ensure_ascii=False, indent=2))
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    return _emit(service.get_recipe(args.id, db_path=DB_PATH))


def cmd_add(args: argparse.Namespace) -> int:
    return _emit(service.add_recipe(
        args.title, _json_arg(args.ingredients_json, "ingredients-json"), _json_arg(args.steps_json, "steps-json"),
        description=args.description, tags=_json_arg(args.tags_json, "tags-json"),
        protein_type=args.protein_type, meal_type=args.meal_type, prep_time_min=args.prep_time_min,
        cook_time_min=args.cook_time_min, total_time_min=args.total_time_min, servings=args.servings,
        source_url=args.source_url, notes=args.notes, db_path=DB_PATH))


# CLI flag -> update_recipe keyword, for the plain (non-JSON) fields; only flags that were passed apply.
_UPDATE_FIELDS = ("title", "description", "protein_type", "meal_type", "prep_time_min", "cook_time_min",
                  "total_time_min", "servings", "source_url", "notes")


def cmd_update(args: argparse.Namespace) -> int:
    changes: dict = {f: getattr(args, f) for f in _UPDATE_FIELDS if getattr(args, f) is not None}
    for flag, key in (("ingredients_json", "ingredients"), ("steps_json", "steps"), ("tags_json", "tags")):
        if getattr(args, flag) is not None:
            changes[key] = _json_arg(getattr(args, flag), flag.replace("_", "-"))
    for field in args.clear or []:
        changes[field] = None
    if not changes:
        raise service.RecipeError("nothing to update: pass at least one field flag or --clear")
    return _emit(service.update_recipe(args.id, db_path=DB_PATH, **changes))


def cmd_delete(args: argparse.Namespace) -> int:
    removed = service.delete_recipe(args.id, db_path=DB_PATH)
    print(json.dumps({"deleted": removed["id"], "title": removed["title"]}, ensure_ascii=False))
    return 0


def cmd_mark_cooked(args: argparse.Namespace) -> int:
    return _emit(service.mark_cooked(args.id, args.date, db_path=DB_PATH))


def cmd_feedback(args: argparse.Namespace) -> int:
    return _emit(service.add_feedback(args.id, args.comment, args.rating, db_path=DB_PATH))


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
    ls.add_argument(
        "--compact", action="store_true",
        help="only id/title/meal_type/protein_type/tags/total_time_min/rating/last_cooked_at "
             "(about 5% of the full size); fetch one recipe's detail with `get --id`",
    )
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
    add.add_argument("--total-time-min", dest="total_time_min", type=int, default=None)
    add.add_argument("--notes", default=None)
    add.set_defaults(func=cmd_add)

    upd = sub.add_parser("update", help="Edit fields of an existing recipe (only passed fields change).")
    upd.add_argument("--id", required=True)
    upd.add_argument("--title", default=None)
    upd.add_argument("--description", default=None)
    upd.add_argument("--ingredients-json", dest="ingredients_json", default=None)
    upd.add_argument("--steps-json", dest="steps_json", default=None)
    upd.add_argument("--tags-json", dest="tags_json", default=None)
    upd.add_argument("--protein-type", dest="protein_type", default=None)
    upd.add_argument("--meal-type", dest="meal_type", default=None)
    upd.add_argument("--prep-time-min", dest="prep_time_min", type=int, default=None)
    upd.add_argument("--cook-time-min", dest="cook_time_min", type=int, default=None)
    upd.add_argument("--total-time-min", dest="total_time_min", type=int, default=None)
    upd.add_argument("--servings", type=int, default=None)
    upd.add_argument("--source-url", dest="source_url", default=None)
    upd.add_argument("--notes", default=None)
    upd.add_argument("--clear", nargs="+", default=None,
                     choices=["description", "protein_type", "prep_time_min", "cook_time_min",
                              "total_time_min", "servings", "source_url", "notes", "tags"],
                     help="set these optional fields back to empty")
    upd.set_defaults(func=cmd_update)

    dele = sub.add_parser("delete", help="Permanently delete a recipe.")
    dele.add_argument("--id", required=True)
    dele.set_defaults(func=cmd_delete)

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
    try:
        return args.func(args)
    except service.RecipeError as exc:
        print(json.dumps({"error": exc.message}), file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
