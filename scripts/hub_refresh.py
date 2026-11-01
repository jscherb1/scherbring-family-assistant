#!/usr/bin/env python3
"""Scripted My Hub refresh: calendar events (from Home Assistant) and Todoist tasks, no agent involved.

Replaces the `myhub` agent copying data in on a schedule. Run by assistant-hub-refresh.timer every 15 minutes
(6am-10pm) and by the myhub agent for "refresh my hub".

  events  every HA calendar, local today .. today+7 inclusive, written with myhub.set_snapshot("event", ...)
  tasks   dated open Todoist tasks (overdue included) minus the shopping lists, myhub.set_snapshot("task", ...)

A failed fetch never replaces a snapshot with less data: the old one stays, the failure is logged and shows in
system health, and notify-check's "could not refresh" caveat covers a long outage. One unreadable calendar skips
the whole event snapshot for that run (a partial write would delete that calendar's events).

Notifications (--notify): after refreshing, run myhub.notify_check and send what it says through notify.send. They
are held back while the `myhub-refresh` scheduled task is enabled, so the cutover is one step: disable that task
(see docs/TODO.md). The dedupe state (hub_notified / hub_notify_meta) is shared with the agent path regardless.

Usage:
    python scripts/hub_refresh.py [--notify] [--dry-run] [--kind event|task|all]
        --dry-run  read everything, print counts and the diff against the stored snapshot, write and send nothing
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import ha  # noqa: E402
import myhub  # noqa: E402
import todoist_api  # noqa: E402

HORIZON_DAYS = 7
EXCLUDED_PROJECT_WORDS = ("shopping", "costco", "staple grocer")
HOLIDAY_WORDS = ("holiday",)
AGENT_TASK_NAME = "myhub-refresh"
LOCK_PATH = REPO_ROOT / "state" / ".hub_refresh.lock"


def log(msg: str) -> None:
    print(f"hub_refresh: {msg}", file=sys.stderr)


# ---- mapping ----

def source_name(name: str) -> str:
    """HA's calendar names are sentence case ("Kids school calendar"); the snapshot has always used title case
    ("Kids School Calendar"). Keep that so rows, and the schedule provider's `meal` prefix check, stay the same."""
    name = (name or "").strip()
    return name if "@" in name else " ".join(w[:1].upper() + w[1:] for w in name.split(" "))


def is_holiday_calendar(cal: dict) -> bool:
    text = f"{cal.get('name', '')} {cal.get('entity_id', '')}".lower()
    return any(w in text for w in HOLIDAY_WORDS)


def is_shopping_project(name: str) -> bool:
    n = (name or "").lower()
    return any(w in n for w in EXCLUDED_PROJECT_WORDS)


def fetch_events(today: date) -> tuple[list[dict], list[str]]:
    """(items, errors). Any error means the items are incomplete and must not be written."""
    last = today + timedelta(days=HORIZON_DAYS)
    try:
        cals = [c for c in ha.list_calendars() if not is_holiday_calendar(c)]
    except ha.HAError as exc:
        return [], [f"calendar list: {exc.message}"]
    items, errors = [], []
    for cal in cals:
        try:
            for e in ha.calendar_window(cal["entity_id"], today, last):
                items.append({"title": e["title"] or "(no title)", "day": e["day"], "start_time": e["start_time"],
                              "end_time": e["end_time"], "source": source_name(cal["name"])})
        except ha.HAError as exc:
            errors.append(f"{cal['name']}: {exc.message}")
    return items, errors


def fetch_tasks() -> tuple[list[dict], list[str]]:
    try:
        projects = {p["id"]: p.get("name", "") for p in todoist_api.list_projects()}
        tasks = todoist_api.list_open_tasks()
    except todoist_api.TodoistError as exc:
        return [], [f"todoist ({exc.code}): {exc.message}"]
    tz = ha.local_tz()
    items = []
    for t in tasks:
        day = todoist_api.due_day(t, tz)
        pname = projects.get(t.get("project_id"), "")
        if not day or is_shopping_project(pname):
            continue
        api_prio = t.get("priority") or 1
        items.append({"title": str(t.get("content") or "").strip() or "(no title)", "day": day,
                      "source": pname or None, "priority": max(1, min(4, 5 - int(api_prio)))})  # p1 (API 4) -> 1
    return items, []


# ---- snapshot compare ----

def _key(kind: str, i: dict) -> tuple:
    if kind == "event":
        return (i["day"], i.get("start_time") or "", i.get("end_time") or "", i["title"], i.get("source") or "")
    return (i["day"], i["title"], i.get("source") or "", i.get("priority"))


def _stored(kind: str, db_path, start=None, end=None) -> list[dict]:
    conn = myhub.connect(db_path)
    try:
        if kind == "event":
            rows = conn.execute("SELECT * FROM hub_items WHERE kind='event' AND day BETWEEN ? AND ?", (start, end))
        else:
            rows = conn.execute("SELECT * FROM hub_items WHERE kind='task'")
        return [dict(r) for r in rows.fetchall()]
    finally:
        conn.close()


def diff(kind: str, new: list[dict], db_path=None, start=None, end=None) -> dict:
    old = _stored(kind, db_path, start, end)
    cleaned = [myhub._check_item(i, kind) for i in new]
    a = {}
    for i in cleaned:
        a[_key(kind, i)] = a.get(_key(kind, i), 0) + 1
    b = {}
    for i in old:
        b[_key(kind, i)] = b.get(_key(kind, i), 0) + 1
    added = [k for k in a if a[k] > b.get(k, 0)]
    removed = [k for k in b if b[k] > a.get(k, 0)]
    return {"stored_now": len(old), "fetched": len(cleaned), "added": len(added), "removed": len(removed),
            "added_items": sorted(added), "removed_items": sorted(removed)}


# ---- notify ----

def notify_muted_reason(db_path=None) -> str | None:
    """Why notifications must stay off (the agent's scheduled task would also send), or None."""
    conn = myhub.connect(db_path)
    try:
        r = conn.execute("SELECT enabled FROM scheduled_tasks WHERE name=?", (AGENT_TASK_NAME,)).fetchone()
    finally:
        conn.close()
    if r and r["enabled"]:
        return f"scheduled task {AGENT_TASK_NAME!r} is still enabled"
    return None


def run_notify(db_path=None, now: datetime | None = None, sender=None) -> dict:
    """Send what notify_check says. Dedupe state is only recorded after delivery succeeds, so a failed send
    is retried on the next run rather than silently lost."""
    muted = notify_muted_reason(db_path)
    if muted:
        return {"sent": False, "skipped": muted}
    res = myhub.notify_check(db_path=db_path, now=now, record=False)
    if not res["send"]:
        return {"sent": False, "skipped": "nothing to say"}
    import notify
    kwargs = {"sender": sender} if sender else {}
    out = notify.send(res["message"], source="hub-refresh", kind="notice", **kwargs)
    if not out.delivered:
        return {"sent": False, "error": out.error or "not delivered"}
    myhub.notify_check(db_path=db_path, now=now, record=True)
    return {"sent": True, "mode": res["mode"]}


# ---- orchestration ----

def _record_health(key: str, errors: list[str], detail_ok: str, db_path) -> None:
    try:
        import system_health
        if errors:
            system_health.record(key, "warn", "; ".join(errors)[:200], db_path=db_path)
        else:
            system_health.record(key, "ok", detail_ok, db_path=db_path)
    except Exception as exc:  # noqa: BLE001 - bookkeeping must not break the refresh
        log(f"could not record {key} health: {exc}")


def refresh(*, db_path=None, today: date | None = None, kinds=("event", "task"), notify: bool = False,
            dry_run: bool = False, now: datetime | None = None) -> dict:
    today = today or (now.date() if now else datetime.now(ha.local_tz()).date())
    start, end = today.isoformat(), (today + timedelta(days=HORIZON_DAYS)).isoformat()
    result: dict = {"dry_run": dry_run}
    if "event" in kinds:
        items, errors = fetch_events(today)
        res = {"errors": errors, "range": [start, end]}
        if errors:
            res["status"] = "kept old snapshot"
            log("events not updated: " + "; ".join(errors))
        else:
            res["status"] = "dry-run" if dry_run else "updated"
            if dry_run:
                res.update(diff("event", items, db_path, start, end))
            else:
                res["stored"] = myhub.set_snapshot("event", items, start=start, end=end, db_path=db_path)["stored"]
        if not dry_run:
            _record_health("hub_calendar", errors, "read via Home Assistant", db_path)
        result["event"] = res
    if "task" in kinds:
        items, errors = fetch_tasks()
        res = {"errors": errors}
        if errors:
            res["status"] = "kept old snapshot"
            log("tasks not updated: " + "; ".join(errors))
        else:
            res["status"] = "dry-run" if dry_run else "updated"
            if dry_run:
                res.update(diff("task", items, db_path))
            else:
                res["stored"] = myhub.set_snapshot("task", items, db_path=db_path)["stored"]
        if not dry_run:
            _record_health("todoist_api", errors, "read via the Todoist API", db_path)
        result["task"] = res
    if notify and not dry_run:
        try:
            result["notify"] = run_notify(db_path, now)
        except Exception as exc:  # noqa: BLE001
            result["notify"] = {"sent": False, "error": str(exc)}
            log(f"notify step failed: {exc}")
    elif notify:
        result["notify"] = {"sent": False, "skipped": "dry run"}
    result["ok"] = not any(result.get(k, {}).get("errors") for k in ("event", "task"))
    return result


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--notify", action="store_true", help="also send the notify-check message (held while the agent task is enabled)")
    p.add_argument("--dry-run", action="store_true", help="print counts and diffs; write and send nothing")
    p.add_argument("--kind", choices=("event", "task", "all"), default="all")
    p.add_argument("--db", help="database file (default: state/agent_results.db)")
    args = p.parse_args(argv)
    kinds = ("event", "task") if args.kind == "all" else (args.kind,)
    lock = None
    if not args.dry_run:
        try:
            import fcntl
            LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
            lock = open(LOCK_PATH, "w")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"ok": False, "error": "another hub refresh is running"}))
            return 0
        except OSError:
            lock = None
    try:
        result = refresh(db_path=args.db, kinds=kinds, notify=args.notify, dry_run=args.dry_run)
    except myhub.HubError as exc:
        print(json.dumps({"error": exc.message}), file=sys.stderr)
        return exc.code
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
