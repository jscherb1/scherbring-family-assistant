"""Jump-to search index: every page in the navigation plus recipes, habits and people, for static/ui.js.

Pure composition of the navigation registry and the existing readers; one unreadable source just drops out.
"""
from urllib.parse import quote

from . import family, habits, recipes

ACTIONS = [
    {"label": "Log today's check-in", "href": "/checkin", "keywords": "rate day rating journal mood"},
    {"label": "Add a recipe", "href": "/meals/new", "keywords": "new recipe meal"},
    {"label": "Kid chart", "href": "/kids", "keywords": "chores kids ruth claire"},
    {"label": "Reconnect a login", "href": "/reconnect", "keywords": "drive hyvee login expired auth"},
    {"label": "Old pages (archive)", "href": "/archive", "keywords": "summary my hub"},
]


def get_index(sections):
    items = []
    for s in sections:
        for p in s["pages"]:
            label = p["label"].replace(" ↗", "")
            items.append({"kind": "page", "label": label, "group": s["label"] + (" · " + p["group"] if p.get("group") else ""),
                          "href": p["href"], "keywords": p.get("group", "")})
    items += [{"kind": "action", "group": "Do", **a} for a in ACTIONS]
    try:
        for r in recipes.list_recipes().get("recipes", []):
            items.append({"kind": "recipe", "label": r["title"], "group": "Recipe", "href": f"/meals/{r['id']}",
                          "keywords": " ".join([r.get("meal_type") or "", r.get("protein_type") or "", *r.get("tags", [])])})
    except Exception:  # noqa: BLE001 - search must still work with a source missing
        pass
    try:
        for h in habits.service.list_status(include_inactive=False, db_path=habits.common.db_path()):
            items.append({"kind": "habit", "label": h["name"], "group": "Habit", "href": "/habits", "keywords": "habit streak"})
    except Exception:  # noqa: BLE001
        pass
    prof = family.get_profile()
    for p in prof.get("people", []) if isinstance(prof, dict) else []:
        items.append({"kind": "person", "label": p["name"], "group": "Person", "href": "/family#q=" + quote(p["name"]),
                      "keywords": p.get("relationship") or ""})
    return {"items": items}
