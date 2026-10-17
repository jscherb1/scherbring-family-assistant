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

DISMISS_MAX_DAYS = 60


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
    dismissed_keys = _dismissed_keys(db_path)
    dismissed = [a for a in attention if a["key"] in dismissed_keys]
    attention = [a for a in attention if a["key"] not in dismissed_keys]
    # Items a provider tags with a "group" (Todoist tasks: always some open) never crowd the strip; they are
    # listed under one collapsed row in "Show all" and don't count toward the strip's totals or "All clear".
    grouped = [a for a in attention if a.get("group")]
    attention = [a for a in attention if not a.get("group")]
    groups = [{"label": label, "items": [a for a in grouped if a["group"] == label]}
              for label in dict.fromkeys(a["group"] for a in grouped)]
    return {"date": today.isoformat(), "generated_at": _now_iso(), "attention": attention, "groups": groups,
            "dismissed": dismissed,
            "counts": {lvl: sum(1 for a in attention if a["level"] == lvl) for lvl in LEVELS},
            "cards": cards, "errors": errors, "snapshot": snapshot_status(db_path=db_path),
            "freshness": freshness(db_path=db_path)}


# Expected refresh interval per data source, in minutes. A source older than twice this shows amber.
FRESH_MINUTES = {"coros_health": 30, "weather": 30, "habits": 30, "calendar": 120, "tasks": 120}


def freshness(*, db_path=None) -> list[dict]:
    """When each live data source last refreshed, and whether that is overdue (older than 2x its interval)."""
    import insights
    snap = snapshot_status(db_path=db_path)
    rows = [("Calendar", "calendar", (snap.get("event") or {}).get("synced_at")),
            ("Tasks", "tasks", (snap.get("task") or {}).get("synced_at"))]
    ins = insights.freshness(db_path=db_path)
    for label, key in (("Sleep & recovery", "coros_health"), ("Weather", "weather"), ("Habits", "habits")):
        rows.append((label, key, ins.get(key)))
    now = datetime.now()
    out = []
    for label, key, at in rows:
        stale = at is None or now - datetime.fromisoformat(at) > timedelta(minutes=2 * FRESH_MINUTES[key])
        out.append({"label": label, "key": key, "at": at, "stale": stale})
    return out


def refresh_live(*, db_path=None) -> dict:
    """Refresh the script-reachable sources now (calendar and tasks need the myhub agent)."""
    import insights
    return insights.refresh_live(db_path=db_path)


# ---- dismissing reviewed items ----

def _dismissed_keys(db_path=None) -> set[str]:
    conn = connect(db_path)
    try:
        return {r["key"] for r in conn.execute("SELECT key FROM hub_dismissed")}
    finally:
        conn.close()


def _all_items(db_path=None, today: date | None = None) -> list[dict]:
    h = get_hub(db_path=db_path, today=today)
    return (h["attention"] + [a for g in h["groups"] for a in g["items"]] + h["dismissed"])


def dismiss(key: str, *, db_path=None, today: date | None = None) -> dict:
    """Hide one needs-attention item (it must be on the hub right now). Also forgets dismissals older
    than DISMISS_MAX_DAYS."""
    items = {a["key"]: a for a in _all_items(db_path, today)}
    if key not in items:
        raise HubError(f"no needs-attention item with key {key!r}", 1)
    cutoff = (datetime.now() - timedelta(days=DISMISS_MAX_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    conn = connect(db_path)
    try:
        for r in conn.execute("SELECT key, dismissed_at FROM hub_dismissed").fetchall():
            if r["dismissed_at"] < cutoff:  # age only: the Summary page's keys aren't hub items
                conn.execute("DELETE FROM hub_dismissed WHERE key=?", (r["key"],))
        conn.execute("INSERT OR REPLACE INTO hub_dismissed (key, text, dismissed_at) VALUES (?,?,?)",
                     (key, items[key]["text"], _now_iso()))
        conn.commit()
    finally:
        conn.close()
    return {"dismissed": key}


def dismiss_key(key: str, text: str = "", *, db_path=None) -> dict:
    """Dismiss by key without requiring the item to be on the hub (the Summary page has its own items). Both
    pages and the notifier read the same table, so a dismissal anywhere hides the item everywhere it appears."""
    if not key:
        raise HubError("a key is required")
    cutoff = (datetime.now() - timedelta(days=DISMISS_MAX_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    conn = connect(db_path)
    try:
        conn.execute("DELETE FROM hub_dismissed WHERE dismissed_at < ?", (cutoff,))
        conn.execute("INSERT OR REPLACE INTO hub_dismissed (key, text, dismissed_at) VALUES (?,?,?)",
                     (key, text, _now_iso()))
        conn.commit()
    finally:
        conn.close()
    return {"dismissed": key}


def dismissed_rows(*, db_path=None) -> dict[str, str]:
    """{key: dismissed_at} for every dismissal."""
    conn = connect(db_path)
    try:
        return {r["key"]: r["dismissed_at"] for r in conn.execute("SELECT key, dismissed_at FROM hub_dismissed")}
    finally:
        conn.close()


def restore(key: str | None = None, *, db_path=None) -> dict:
    """Un-dismiss one item, or all of them when key is None."""
    conn = connect(db_path)
    try:
        if key is None:
            n = conn.execute("DELETE FROM hub_dismissed").rowcount
        else:
            n = conn.execute("DELETE FROM hub_dismissed WHERE key=?", (key,)).rowcount
        conn.commit()
    finally:
        conn.close()
    return {"restored": n}


# ---- notifications: tell the user about each thing once, not on every refresh ----

AWAKE_FROM, AWAKE_UNTIL = 7, 20          # outside these hours only a NEW urgent item gets through
URGENT_REPEAT_HOURS = 6
NOTIFY_LEVELS = ("urgent", "warn")       # info items live on the page and never message


def _meta(conn, name):
    r = conn.execute("SELECT value FROM hub_notify_meta WHERE name=?", (name,)).fetchone()
    return r["value"] if r else None


def _line(a: dict) -> str:
    return f"- {a['text']}"


def notify_check(*, db_path=None, now: datetime | None = None, record: bool = True) -> dict:
    """Decide what (if anything) the scheduled refresh should message the user, and remember it.

    Rules
      - dismissed items (from the hub or the Summary page, one shared table) never notify;
      - each item is announced once when it is new, and again only if it escalates (info -> warn -> urgent);
      - urgent items repeat every URGENT_REPEAT_HOURS while awake; warn items are only repeated by the digest;
      - info items never message;
      - outside AWAKE_FROM..AWAKE_UNTIL only a new or escalated urgent item is sent; the rest waits;
      - the first refresh at/after AWAKE_FROM each day sends one digest of everything still open
        (this replaces that run's other messages);
      - an item that resolves or is dismissed is forgotten, so if it comes back it counts as new.
    Returns {"send": bool, "mode": "digest"|"update"|None, "new", "escalated", "reminders", "message"}.
    """
    now = now or datetime.now()
    hub = get_hub(db_path=db_path, today=now.date())
    items = [a for a in hub["attention"] + [x for g in hub["groups"] for x in g["items"]]
             if a["level"] in NOTIFY_LEVELS]
    items.sort(key=lambda a: (LEVELS[a["level"]], a["area"], a["text"]))
    awake = AWAKE_FROM <= now.hour < AWAKE_UNTIL
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S")
    conn = connect(db_path)
    try:
        rows = {r["key"]: dict(r) for r in conn.execute("SELECT * FROM hub_notified")}
        live = {a["key"] for a in items}
        for k in set(rows) - live:  # resolved, dismissed or downgraded to info: forget it
            conn.execute("DELETE FROM hub_notified WHERE key=?", (k,))
            rows.pop(k)
        today = now.date().isoformat()
        digest_due = awake and now.hour >= AWAKE_FROM and _meta(conn, "digest_date") != today
        new, escalated, reminders = [], [], []
        for a in items:
            row = rows.get(a["key"])
            if row is None:
                new.append(a)
            elif LEVELS[a["level"]] < LEVELS[row["level"]]:
                escalated.append(a)
            elif a["level"] == "urgent":
                last = datetime.fromisoformat(row["last_notified_at"])
                if now - last >= timedelta(hours=URGENT_REPEAT_HOURS):
                    reminders.append(a)
        if digest_due:
            mode, told = "digest", items
        else:
            gate = (lambda a: a["level"] == "urgent") if not awake else (lambda a: True)
            new = [a for a in new if gate(a)]
            escalated = [a for a in escalated if gate(a)]
            reminders = reminders if awake else []
            told = new + escalated + reminders
            mode = "update" if told else None
        if record and (told or digest_due):
            for a in told:
                conn.execute(
                    "INSERT INTO hub_notified (key, level, first_notified_at, last_notified_at) VALUES (?,?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET level=excluded.level, last_notified_at=excluded.last_notified_at",
                    (a["key"], a["level"], stamp, stamp))
            if digest_due:
                conn.execute("INSERT OR REPLACE INTO hub_notify_meta (name, value) VALUES ('digest_date', ?)", (today,))
        if record:
            conn.commit()
    finally:
        conn.close()
    message = None
    if mode == "digest":
        message = ("Good morning. Still open on your hub:\n" + "\n".join(_line(a) for a in items)) if items else None
    elif mode == "update":
        parts = []
        if new or escalated:
            parts.append("New on your hub:\n" + "\n".join(_line(a) for a in new + escalated))
        if reminders:
            parts.append("Still urgent:\n" + "\n".join(_line(a) for a in reminders))
        message = "\n\n".join(parts)
    return {"send": bool(message), "mode": mode if message else None, "message": message,
            "new": [a["key"] for a in new], "escalated": [a["key"] for a in escalated],
            "reminders": [a["key"] for a in reminders]}
