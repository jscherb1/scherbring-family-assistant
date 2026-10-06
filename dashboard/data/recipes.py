"""Recipe browsing (read-only) over the `recipes` table in agent_results.db."""
import json
import sqlite3

from . import common

SORTS = {
    "title": lambda r: r["title"].lower(),
    "rating": lambda r: (-(r["rating"] or 0), r["title"].lower()),
    "time": lambda r: (r["total_time_min"] is None, r["total_time_min"] or 0, r["title"].lower()),
    "cooked": lambda r: (r["last_cooked_at"] is None, _neg(r["last_cooked_at"]), r["title"].lower()),
}


def _neg(date_text):
    """Sort key putting the most recent date first."""
    return "".join(chr(0x10FFFF - ord(c)) for c in date_text) if date_text else ""


def _loads(text, default):
    if not text:
        return default
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return default
    return value if isinstance(value, type(default)) else default


def ingredient_text(item) -> str:
    """Display string for either ingredient shape: {"text"} or {"item","quantity","unit"}."""
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return ""
    if item.get("text"):
        return str(item["text"])
    parts = [item.get("quantity"), item.get("unit"), item.get("item")]
    return " ".join(str(p) for p in parts if p not in (None, ""))


def _shape(row, full: bool):
    ingredients = [
        {"text": ingredient_text(i),
         "shopping": bool(i.get("include_in_shopping_list", True)) if isinstance(i, dict) else True}
        for i in _loads(row["ingredients_json"], [])
    ]
    ingredients = [i for i in ingredients if i["text"]]
    recipe = {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "meal_type": row["meal_type"],
        "protein_type": row["protein_type"],
        "tags": _loads(row["tags_json"], []),
        "prep_time_min": row["prep_time_min"],
        "cook_time_min": row["cook_time_min"],
        "total_time_min": row["total_time_min"],
        "servings": row["servings"],
        "rating": row["rating"],
        "last_cooked_at": row["last_cooked_at"],
        "source_url": row["source_url"],
        "ingredient_count": len(ingredients),
        "_search": " ".join([row["title"] or "", row["description"] or "",
                             " ".join(_loads(row["tags_json"], [])),
                             " ".join(i["text"] for i in ingredients)]).lower(),
    }
    if full:
        recipe.update({
            "ingredients": ingredients,
            "steps": [s for s in _loads(row["steps_json"], []) if isinstance(s, str)],
            "notes": row["notes"],
            "feedback": sorted(
                [f for f in _loads(row["feedback_json"], []) if isinstance(f, dict)],
                key=lambda f: f.get("date") or "", reverse=True),
        })
    return recipe


def _all_rows():
    conn = common.connect_ro()
    try:
        return conn.execute("SELECT * FROM recipes").fetchall()
    finally:
        conn.close()


def list_recipes(q: str = "", meal_type: str = "", protein: str = "", tag: str = "",
                 sort: str = "title"):
    """Filtered, sorted recipe cards plus the facet values for the filter controls."""
    try:
        rows = _all_rows()
    except (FileNotFoundError, sqlite3.OperationalError) as exc:
        return {"error": str(exc), "recipes": [], "facets": {}}

    recipes = [_shape(r, full=False) for r in rows]
    facets = {
        "meal_types": sorted({r["meal_type"] for r in recipes if r["meal_type"]}),
        "proteins": sorted({r["protein_type"] for r in recipes if r["protein_type"]}),
        "tags": sorted({t for r in recipes for t in r["tags"]}),
    }

    terms = (q or "").lower().split()
    out = []
    for r in recipes:
        if meal_type and r["meal_type"] != meal_type:
            continue
        if protein and r["protein_type"] != protein:
            continue
        if tag and tag not in r["tags"]:
            continue
        if terms and not all(t in r["_search"] for t in terms):
            continue
        out.append(r)
    out.sort(key=SORTS.get(sort, SORTS["title"]))
    for r in out:
        r.pop("_search", None)
    return {"recipes": out, "facets": facets, "total": len(recipes)}


def get_recipe(recipe_id: str):
    """Full recipe, or None if it doesn't exist."""
    conn = common.connect_ro()
    try:
        row = conn.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    recipe = _shape(row, full=True)
    recipe.pop("_search", None)
    return recipe
