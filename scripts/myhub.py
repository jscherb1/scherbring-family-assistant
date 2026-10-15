"""My Hub service: one view of everything that matters to the person.

Zero-dependency (Python stdlib only), same SQLite file as the rest of the assistant. The agent
(`myhub_store.py`) and the dashboard (`/me`) both call get_hub(), so they always agree.

How it stays complete
  - Every module in scripts/hub_providers/ (names not starting with "_") is a provider:
        PROVIDER = {"name": ..., "title": ...}
        collect(conn, today) -> {"card": {...}, "attention": [{key, text, href, level, area}]}
    A new feature adds one provider and its needs-attention items flow into the strip on the hub with
    no other wiring. REQUIRED_PROVIDERS is checked by a test so a feature can't quietly fall off the hub.
  - A provider that raises is never dropped silently: it becomes a visible "unavailable" attention item.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import sqlite3
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import dayrating  # noqa: E402

LEVELS = {"urgent": 0, "warn": 1, "info": 2}
KINDS = ("event", "task")
STALE_HOURS = 36
REQUIRED_PROVIDERS = ["habits", "mood", "body", "schedule", "weather", "home", "chores", "system", "insights"]


class HubError(Exception):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.message = message
        self.code = code


def connect(db_path=None) -> sqlite3.Connection:
    return dayrating.connect(db_path)


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _today(today: date | None) -> date:
    return today or datetime.now().date()


def discover_providers() -> list:
    from importlib import import_module
    import hub_providers
    mods = []
    for info in pkgutil.iter_modules(hub_providers.__path__):
        if info.name.startswith("_"):
            continue
        mod = import_module(f"hub_providers.{info.name}")
        if hasattr(mod, "PROVIDER") and hasattr(mod, "collect"):
            mods.append(mod)
    return mods


# ---- snapshot of events and tasks (written by the myhub agent) ----

def _check_item(item: dict, kind: str) -> dict:
    title = str(item.get("title") or "").strip()
    day = str(item.get("day") or "")[:10]
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        raise HubError(f"item {title!r} has an invalid day {day!r}, expected YYYY-MM-DD") from None
    if not title:
        raise HubError("every item needs a title")
    prio = item.get("priority")
    if prio is not None and (not isinstance(prio, int) or not 1 <= prio <= 4):
        raise HubError(f"priority must be 1-4, got {prio!r}")
    return {"day": day, "start_time": item.get("start_time") or None, "end_time": item.get("end_time") or None,
            "title": title, "source": item.get("source") or None, "detail": item.get("detail") or None,
            "priority": prio}


def set_snapshot(kind: str, items: list[dict], *, start: str | None = None, end: str | None = None,
                 db_path=None) -> dict:
    """Replace the stored items of `kind`. Events replace the start..end day range (required); tasks
    replace everything (the agent passes all open tasks). Validates everything before touching the table."""
    if kind not in KINDS:
        raise HubError(f"kind must be one of {', '.join(KINDS)}")
    if not isinstance(items, list):
        raise HubError("items must be a JSON list")
    clean = [_check_item(i, kind) for i in items]
    if kind == "event":
        if not (start and end):
            raise HubError("events need --start and --end (the range this snapshot covers)")
        for d in (start, end):
            try:
                datetime.strptime(d, "%Y-%m-%d")
            except ValueError:
                raise HubError(f"invalid date {d!r}, expected YYYY-MM-DD") from None
    conn = connect(db_path)
    try:
        if kind == "event":
            conn.execute("DELETE FROM hub_items WHERE kind='event' AND day BETWEEN ? AND ?", (start, end))
        else:
            conn.execute("DELETE FROM hub_items WHERE kind='task'")
        now = _now_iso()
        for c in clean:
            conn.execute(
                "INSERT INTO hub_items (id, kind, day, start_time, end_time, title, source, detail, priority, synced_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), kind, c["day"], c["start_time"], c["end_time"], c["title"], c["source"],
                 c["detail"], c["priority"], now))
        conn.commit()
        return {"kind": kind, "stored": len(clean), "synced_at": now}
    finally:
        conn.close()


def snapshot_status(*, db_path=None) -> dict:
    conn = connect(db_path)
    try:
        out = {}
        for k in KINDS:
            r = conn.execute("SELECT COUNT(*) n, MAX(synced_at) at, MIN(day) lo, MAX(day) hi FROM hub_items "
                             "WHERE kind=?", (k,)).fetchone()
            out[k] = {"count": r["n"], "synced_at": r["at"], "from": r["lo"], "to": r["hi"]}
        return out
    finally:
        conn.close()


# ---- the hub ----

def get_hub(*, db_path=None, today: date | None = None) -> dict:
    today = _today(today)
    conn = connect(db_path)
    cards, attention, errors = {}, [], {}
    try:
        for mod in discover_providers():
            name = mod.PROVIDER["name"]
            try:
                res = mod.collect(conn, today) or {}
            except Exception as exc:  # a broken area must be visible, never silently missing
                errors[name] = str(exc)
                attention.append({"key": f"hub:{name}:error", "area": mod.PROVIDER.get("title", name),
                                  "text": f"{mod.PROVIDER.get('title', name)} couldn't be read ({exc})",
                                  "href": "/me", "level": "warn"})
                continue
            cards[name] = {"title": mod.PROVIDER.get("title", name), **(res.get("card") or {})}
            for a in res.get("attention") or []:
                attention.append({"area": mod.PROVIDER.get("title", name), "level": "warn", **a})
    finally:
        conn.close()
    attention.sort(key=lambda a: (LEVELS.get(a["level"], 1), a["area"], a["text"]))
    # Items a provider tags with a "group" (Todoist tasks: always some open) never crowd the strip; they are
    # listed under one collapsed row in "Show all" and don't count toward the strip's totals or "All clear".
    grouped = [a for a in attention if a.get("group")]
    attention = [a for a in attention if not a.get("group")]
    groups = [{"label": label, "items": [a for a in grouped if a["group"] == label]}
              for label in dict.fromkeys(a["group"] for a in grouped)]
    return {"date": today.isoformat(), "generated_at": _now_iso(), "attention": attention, "groups": groups,
            "counts": {lvl: sum(1 for a in attention if a["level"] == lvl) for lvl in LEVELS},
            "cards": cards, "errors": errors, "snapshot": snapshot_status(db_path=db_path)}
