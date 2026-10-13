"""Recipe library: the single shared service.

Zero-dependency (Python stdlib only), same SQLite file as state_store.py
(state/agent_results.db, schema in state/schema.sql).

Both front ends are thin wrappers over this module, so there is exactly one
implementation of the recipe rules (validation, add / edit / delete, cooking history, feedback):
  - scripts/recipes_store.py   the CLI the `meal-planner` subagent runs
  - dashboard/data/recipes.py + dashboard/routers/meals.py   the web UI and its /api/recipes* twins

Every function takes an optional `db_path` (default: state/agent_results.db). Failures raise
RecipeError with a CLI-style code: 1 = not found, 2 = bad input.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "state" / "agent_results.db"
SCHEMA_PATH = REPO_ROOT / "state" / "schema.sql"

PANTRY_SUFFIX = " (pantry)"
_JSON_COLUMNS = ("ingredients_json", "steps_json", "tags_json", "feedback_json")
_INT_FIELDS = ("prep_time_min", "cook_time_min", "total_time_min", "servings")
_TEXT_FIELDS = ("description", "protein_type", "meal_type", "source_url", "notes")


class RecipeError(Exception):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.message = message
        self.code = code


# ---------------------------------------------------------------------------
# plumbing
# ---------------------------------------------------------------------------


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for key in _JSON_COLUMNS:
        if d.get(key):
            d[key] = json.loads(d[key])
    return d


COMPACT_FIELDS = (
    "id", "title", "meal_type", "protein_type", "tags_json",
    "total_time_min", "rating", "last_cooked_at",
)


def compact(row: dict) -> dict:
    """Just what is needed to choose candidates; use get_recipe for full detail."""
    out = {k: row.get(k) for k in COMPACT_FIELDS}
    out["tags"] = out.pop("tags_json") or []
    return out


def _row(conn: sqlite3.Connection, recipe_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if row is None:
        raise RecipeError(f"no recipe with id {recipe_id}", 1)
    return row


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def _clean_title(title) -> str:
    title = (title or "").strip()
    if not title:
        raise RecipeError("title is required")
    return title


def _clean_text(value) -> str | None:
    return (str(value).strip() or None) if value is not None else None


def _clean_int(value, field: str) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise RecipeError(f"{field} must be a whole number") from None
    if number < 0:
        raise RecipeError(f"{field} can't be negative")
    return number


def _clean_list(value, field: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise RecipeError(f"{field} must be a list")
    return value


def _clean_steps(value) -> list[str]:
    steps = _clean_list(value, "steps")
    if not all(isinstance(s, str) for s in steps):
        raise RecipeError("steps must be a list of text")
    return [s.strip() for s in steps if s.strip()]


def _clean_ingredients(value) -> list:
    items = _clean_list(value, "ingredients")
    if not all(isinstance(i, (str, dict)) for i in items):
        raise RecipeError("ingredients must be a list of text or objects")
    return [{"text": i.strip(), "include_in_shopping_list": True} if isinstance(i, str) else i
            for i in items if not isinstance(i, str) or i.strip()]


def _clean_tags(value) -> list[str]:
    tags = _clean_list(value, "tags")
    if not all(isinstance(t, str) for t in tags):
        raise RecipeError("tags must be a list of text")
    return [t.strip() for t in tags if t.strip()]


def _check_date(value: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        raise RecipeError(f"invalid date {value!r}, expected YYYY-MM-DD") from None
    return value


def _check_rating(rating):
    if rating is not None and not (isinstance(rating, int) and 1 <= rating <= 5):
        raise RecipeError("rating must be a whole number from 1 to 5")
    return rating


def _total(prep, cook, total):
    """Total time defaults to prep + cook when it isn't given."""
    if total is None and (prep is not None or cook is not None):
        return (prep or 0) + (cook or 0)
    return total


# ---------------------------------------------------------------------------
# text <-> list helpers (the dashboard form edits ingredients/steps/tags as plain text)
# ---------------------------------------------------------------------------


def ingredients_from_text(text: str) -> list[dict]:
    """One ingredient per line; a trailing "(pantry)" keeps it off the shopping list."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        tag = PANTRY_SUFFIX.strip()
        pantry = line.lower().endswith(tag) and len(line) > len(tag)
        if pantry:
            line = line[: -len(tag)].strip()
        out.append({"text": line, "include_in_shopping_list": not pantry})
    return out


def lines_from_text(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def tags_from_text(text: str) -> list[str]:
    return [t.strip() for t in (text or "").split(",") if t.strip()]


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def list_recipes(*, meal_type=None, protein_type=None, tag=None, search=None, exclude_cooked_since=None,
                 min_rating=None, limit=None, db_path=None) -> list[dict]:
    sql = "SELECT * FROM recipes WHERE 1=1"
    params: list = []
    if meal_type:
        sql += " AND meal_type = ?"
        params.append(meal_type)
    if protein_type:
        sql += " AND protein_type = ?"
        params.append(protein_type)
    if tag:
        sql += " AND tags_json LIKE ?"
        params.append(f'%"{tag}"%')
    if search:
        sql += " AND (title LIKE ? OR description LIKE ?)"
        like = f"%{search}%"
        params.extend([like, like])
    if exclude_cooked_since:
        sql += " AND (last_cooked_at IS NULL OR last_cooked_at < ?)"
        params.append(exclude_cooked_since)
    if min_rating is not None:
        sql += " AND (rating IS NULL OR rating >= ?)"
        params.append(min_rating)
    sql += " ORDER BY title"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    conn = connect(db_path)
    try:
        return [row_to_dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def get_recipe(recipe_id: str, *, db_path=None) -> dict:
    conn = connect(db_path)
    try:
        return row_to_dict(_row(conn, recipe_id))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------


def add_recipe(title: str, ingredients: list, steps: list, *, description=None, tags=None,
               protein_type=None, meal_type="dinner", prep_time_min=None, cook_time_min=None,
               total_time_min=None, servings=None, source_url=None, notes=None, db_path=None) -> dict:
    title = _clean_title(title)
    ingredients = _clean_ingredients(ingredients)
    steps = _clean_steps(steps)
    tags = _clean_tags(tags) if tags is not None else None
    prep = _clean_int(prep_time_min, "prep time")
    cook = _clean_int(cook_time_min, "cook time")
    total = _total(prep, cook, _clean_int(total_time_min, "total time"))
    new_id = str(uuid.uuid4())
    conn = connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO recipes (
                id, title, description, ingredients_json, steps_json, tags_json,
                protein_type, meal_type, prep_time_min, cook_time_min, total_time_min,
                servings, source_url, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (new_id, title, _clean_text(description),
             json.dumps(ingredients, ensure_ascii=False), json.dumps(steps, ensure_ascii=False),
             json.dumps(tags, ensure_ascii=False) if tags is not None else None,
             _clean_text(protein_type), _clean_text(meal_type), prep, cook, total,
             _clean_int(servings, "servings"), _clean_text(source_url), _clean_text(notes)),
        )
        conn.commit()
        return row_to_dict(_row(conn, new_id))
    finally:
        conn.close()


_UNSET = object()


def update_recipe(recipe_id: str, *, title=_UNSET, description=_UNSET, ingredients=_UNSET, steps=_UNSET,
                  tags=_UNSET, protein_type=_UNSET, meal_type=_UNSET, prep_time_min=_UNSET,
                  cook_time_min=_UNSET, total_time_min=_UNSET, servings=_UNSET, source_url=_UNSET,
                  notes=_UNSET, db_path=None) -> dict:
    """Update any subset of fields; pass None to clear an optional one. If prep/cook change and
    total isn't given, total is recomputed as prep + cook."""
    values = {
        "title": title, "description": description, "ingredients": ingredients, "steps": steps,
        "tags": tags, "protein_type": protein_type, "meal_type": meal_type,
        "prep_time_min": prep_time_min, "cook_time_min": cook_time_min,
        "total_time_min": total_time_min, "servings": servings, "source_url": source_url, "notes": notes,
    }
    conn = connect(db_path)
    try:
        current = _row(conn, recipe_id)
        sets: dict = {}
        if title is not _UNSET:
            sets["title"] = _clean_title(title)
        if ingredients is not _UNSET:
            sets["ingredients_json"] = json.dumps(_clean_ingredients(ingredients), ensure_ascii=False)
        if steps is not _UNSET:
            sets["steps_json"] = json.dumps(_clean_steps(steps), ensure_ascii=False)
        if tags is not _UNSET:
            sets["tags_json"] = json.dumps(_clean_tags(tags), ensure_ascii=False) if tags is not None else None
        for field in _TEXT_FIELDS:
            if values[field] is not _UNSET:
                sets[field] = _clean_text(values[field])
        for field in _INT_FIELDS:
            if values[field] is not _UNSET:
                sets[field] = _clean_int(values[field], field.replace("_min", "").replace("_", " "))
        if total_time_min is _UNSET and (prep_time_min is not _UNSET or cook_time_min is not _UNSET):
            prep = sets.get("prep_time_min", current["prep_time_min"])
            cook = sets.get("cook_time_min", current["cook_time_min"])
            sets["total_time_min"] = _total(prep, cook, None)
        if sets:
            assignments = ", ".join(f"{col} = ?" for col in sets)
            conn.execute(f"UPDATE recipes SET {assignments} WHERE id = ?", (*sets.values(), recipe_id))
            conn.commit()
        return row_to_dict(_row(conn, recipe_id))
    finally:
        conn.close()


def delete_recipe(recipe_id: str, *, db_path=None) -> dict:
    """Permanently remove a recipe; returns the removed recipe."""
    conn = connect(db_path)
    try:
        removed = row_to_dict(_row(conn, recipe_id))
        conn.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
        conn.commit()
        return removed
    finally:
        conn.close()


def mark_cooked(recipe_id: str, date: str, *, db_path=None) -> dict:
    _check_date(date)
    conn = connect(db_path)
    try:
        _row(conn, recipe_id)
        conn.execute("UPDATE recipes SET last_cooked_at = ? WHERE id = ?", (date, recipe_id))
        conn.commit()
        return row_to_dict(_row(conn, recipe_id))
    finally:
        conn.close()


def add_feedback(recipe_id: str, comment: str, rating: int | None = None, *, db_path=None) -> dict:
    comment = (comment or "").strip()
    if not comment:
        raise RecipeError("comment is required")
    _check_rating(rating)
    conn = connect(db_path)
    try:
        row = _row(conn, recipe_id)
        feedback = json.loads(row["feedback_json"]) if row["feedback_json"] else []
        feedback.append({"date": datetime.now().strftime("%Y-%m-%d"), "comment": comment, "rating": rating})
        conn.execute(
            "UPDATE recipes SET feedback_json = ?, rating = ? WHERE id = ?",
            (json.dumps(feedback, ensure_ascii=False), rating if rating is not None else row["rating"], recipe_id),
        )
        conn.commit()
        return row_to_dict(_row(conn, recipe_id))
    finally:
        conn.close()
