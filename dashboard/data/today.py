"""Today page reader: the merged home page (what used to be Summary + My Hub).

Pure composition, no rules of its own:
- Cards and the ranked needs-attention list come from the hub service (scripts/myhub.py, via data/myhub.py), the
  same output the `myhub` agent and the hub's Telegram notifications use.
- The Summary reader (data/summary.py) adds the glance tiles and the two kinds of attention item the hub doesn't
  raise: agent problems (`agent:*`) and logins close to expiry (`cred:*`). Everything else it flags, the hub
  already covers.
Dismissals are the one shared store (`hub_dismissed`), so hiding an item here hides it everywhere.
"""
from . import myhub, summary

LEVELS = {"urgent": 0, "warn": 1, "info": 2}
SUMMARY_ONLY = ("agent:", "cred:")
TILE_TAB = {
    "Wellness": "Me", "Habits": "Me", "Fitness": "Me",
    "Chores": "Family", "Family": "Family",
    "Dinner": "Home", "Home & yard": "Home", "Hy-Vee": "Home",
    "Finance": "Money",
    "Agent": "System", "Scheduled tasks": "System",
}
TAB_ORDER = ["Me", "Family", "Home", "Money", "System"]


def _extra(items):
    return [{"key": a["key"], "text": a["text"], "href": a["href"], "area": "System", "level": "warn"}
            for a in items if a["key"].startswith(SUMMARY_ONLY)]


def _rank(items):
    return sorted(items, key=lambda a: (LEVELS.get(a["level"], 1), a["area"], a["text"]))


def get_today():
    hub = myhub.get_hub()
    if "error" in hub:
        return hub
    summ = summary.get_summary()
    attention = _rank(hub["attention"] + _extra(summ.get("all_attention", [])))
    dismissed = hub["dismissed"] + _extra(summ.get("dismissed", []))
    glance = {tab: [] for tab in TAB_ORDER}
    for t in summ.get("tiles", []):
        glance[TILE_TAB.get(t["title"], "System")].append(t)
    return {
        "date": hub["date"],
        "attention": attention,
        "groups": hub["groups"],
        "dismissed": dismissed,
        "counts": {lvl: sum(1 for a in attention if a["level"] == lvl) for lvl in LEVELS},
        "cards": hub["cards"],
        "glance": [{"tab": tab, "tiles": tiles} for tab, tiles in glance.items() if tiles],
        "freshness": hub.get("freshness", []),
        "snapshot": hub.get("snapshot"),
    }


def find_item(key: str) -> dict | None:
    """The attention item (shown or dismissed) with this key, for the dismiss route's stored text."""
    data = get_today()
    if "error" in data:
        return None
    every = data["attention"] + data["dismissed"] + [a for g in data["groups"] for a in g["items"]]
    return next((a for a in every if a["key"] == key), None)
