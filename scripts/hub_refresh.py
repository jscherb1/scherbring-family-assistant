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
# HA also exposes a Birthdays calendar that Google's own calendar list (what the old agent read) never had, so it
# would be new on the dashboard. Keep the set as before.
EXCLUDED_CALENDARS = ("calendar.birthdays",)
# Guard against a "successful" but wrong read replacing a good snapshot.
MIN_STORED_FOR_GUARD = 5      # an empty read is only suspicious when at least this many items are stored
SHARP_DROP_MIN_STORED = 10    # a sharp drop is only judged when at least this many are stored
SHARP_DROP_RATIO = 0.4        # ... and the new count is below this share of the stored count
ACCEPT_AFTER = 4              # consecutive refused reads (about 1 hour at 15 min) after which the new read is accepted
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
    if cal.get("entity_id") in EXCLUDED_CALENDARS:
        return True
    text = f"{cal.get('name', '')} {cal.get('entity_id', '')}".lower()
    return any(w in text for w in HOLIDAY_WORDS)


def is_shopping_project(name: str) -> bool:
    n = (name or "").lower()
    return any(w in n for w in EXCLUDED_PROJECT_WORDS)


def _err_text(exc: Exception) -> str:
    return getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}"


def fetch_events(today: date, db_path=None, force: bool = False) -> tuple[list[dict], list[str], list[str]]:
    """(items, errors, calendar_ids). Any error means the items are incomplete and must not be written.
    A calendar read on an earlier run that HA no longer lists is an error too (unless force)."""
    last = today + timedelta(days=HORIZON_DAYS)
    try:
        cals = [c for c in ha.list_calendars() if not is_holiday_calendar(c)]
    except Exception as exc:  # noqa: BLE001
        return [], [f"calendar list: {_err_text(exc)}"], []
    ids = sorted(c["entity_id"] for c in cals)
    items, errors = [], []
    if not force:
        gone = sorted(set(meta_get("refresh_calendars", db_path) or []) - set(ids))
        if gone:
            errors.append("calendar no longer listed by Home Assistant: " + ", ".join(gone))
    for cal in cals:
        try:
            for e in ha.calendar_window(cal["entity_id"], today, last):
                items.append({"title": e["title"] or "(no title)", "day": e["day"], "start_time": e["start_time"],
                              "end_time": e["end_time"], "source": source_name(cal["name"])})
        except Exception as exc:  # noqa: BLE001 - one malformed event must not kill the run
            errors.append(f"{cal['name']}: {_err_text(exc)}")
    return items, errors, ids


# ---- small persisted state (hub_notify_meta holds name/value strings) ----

def meta_get(name: str, db_path=None, default=None):
    conn = myhub.connect(db_path)
    try:
        r = conn.execute("SELECT value FROM hub_notify_meta WHERE name=?", (name,)).fetchone()
    finally:
        conn.close()
    if not r:
        return default
    try:
        return json.loads(r["value"])
    except ValueError:
        return default


def meta_set(name: str, value, db_path=None) -> None:
    conn = myhub.connect(db_path)
    try:
        conn.execute("INSERT OR REPLACE INTO hub_notify_meta (name, value) VALUES (?, ?)", (name, json.dumps(value)))
        conn.commit()
    finally:
        conn.close()


def shrink_reason(kind: str, new_count: int, stored_count: int, *, force: bool, dry_run: bool, db_path=None) -> str | None:
    """Why a successful read should NOT replace the stored snapshot, or None to go ahead.

    Refuses an empty read over MIN_STORED_FOR_GUARD+ items and a drop below SHARP_DROP_RATIO of SHARP_DROP_MIN_STORED+
    items. Replacing a task snapshot with nothing would also make notify_check forget every notified item and
    announce them all again on the next good read. A genuinely empty list must not wedge forever, so after
    ACCEPT_AFTER refused reads in a row (about an hour at 15 minutes) the new read is accepted; `--force` accepts
    at once. The count of refusals lives in hub_notify_meta and is reset by any accepted read."""
    key = f"refresh_shrunk_{kind}"
    suspicious = (not force) and (
        (new_count == 0 and stored_count >= MIN_STORED_FOR_GUARD)
        or (stored_count >= SHARP_DROP_MIN_STORED and new_count < stored_count * SHARP_DROP_RATIO))
    prior = int(meta_get(key, db_path, 0) or 0)
    if not suspicious:
        if prior and not dry_run:
            meta_set(key, 0, db_path)
        return None
    n = prior + 1
    if n >= ACCEPT_AFTER:
        if not dry_run:
            meta_set(key, 0, db_path)
        log(f"{kind}: accepting a smaller read ({new_count} vs {stored_count} stored) after {n} in a row")
        return None
    if not dry_run:
        meta_set(key, n, db_path)
    return (f"read returned {new_count} {kind}s but {stored_count} are stored; kept the old snapshot "
            f"({n} of {ACCEPT_AFTER} before it is accepted; --force to accept now)")


def fetch_tasks() -> tuple[list[dict], list[str]]:
    try:
        projects = {p["id"]: p.get("name", "") for p in todoist_api.list_projects()}
        tasks = todoist_api.list_open_tasks()
    except todoist_api.TodoistError as exc:
        return [], [f"todoist ({exc.code}): {exc.message}"]
    except Exception as exc:  # noqa: BLE001
        return [], [f"todoist: {_err_text(exc)}"]
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
        err = out.error or "not delivered"
        _record_health("hub_notify", [f"Telegram send failed: {err}"], "", db_path)  # warn only: no alert, retried next run
        return {"sent": False, "error": err}
    myhub.notify_check(db_path=db_path, now=now, record=True)
    _record_health("hub_notify", [], "last notice delivered", db_path)
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
            dry_run: bool = False, now: datetime | None = None, force: bool = False) -> dict:
    today = today or (now.date() if now else datetime.now(ha.local_tz()).date())
    start, end = today.isoformat(), (today + timedelta(days=HORIZON_DAYS)).isoformat()
    result: dict = {"dry_run": dry_run}
    for kind, health_key, health_ok in (("event", "hub_calendar", "read via Home Assistant"),
                                        ("task", "todoist_api", "read via the Todoist API")):
        if kind not in kinds:
            continue
        res: dict = {"errors": []}
        if kind == "event":
            res["range"] = [start, end]
        try:  # one kind failing in any way never blocks the other, nor the health record below
            ids: list[str] = []
            if kind == "event":
                items, errors, ids = fetch_events(today, db_path, force)
            else:
                items, errors = fetch_tasks()
            res["errors"] = list(errors)
            if not errors:
                stored_now = len(_stored(kind, db_path, start, end))
                why = shrink_reason(kind, len(items), stored_now, force=force, dry_run=dry_run, db_path=db_path)
                if why:
                    res["errors"].append(why)
            if res["errors"]:
                res["status"] = "kept old snapshot"
                log(f"{kind}s not updated: " + "; ".join(res["errors"]))
            elif dry_run:
                res["status"] = "dry-run"
                res.update(diff(kind, items, db_path, start, end))
            else:
                if kind == "event":
                    res["stored"] = myhub.set_snapshot("event", items, start=start, end=end, db_path=db_path)["stored"]
                    meta_set("refresh_calendars", ids, db_path)
                else:
                    res["stored"] = myhub.set_snapshot("task", items, db_path=db_path)["stored"]
                res["status"] = "updated"
        except Exception as exc:  # noqa: BLE001
            res["errors"].append(f"{kind} refresh crashed: {_err_text(exc)}")
            res["status"] = "kept old snapshot"
            log(res["errors"][-1])
        if not dry_run:
            _record_health(health_key, res["errors"], health_ok, db_path)
        result[kind] = res
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
    p.add_argument("--force", action="store_true",
                   help="accept an empty or much smaller read, or a calendar that disappeared (e.g. a genuinely empty list)")
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
        result = refresh(db_path=args.db, kinds=kinds, notify=args.notify, dry_run=args.dry_run, force=args.force)
    except myhub.HubError as exc:
        print(json.dumps({"error": exc.message}), file=sys.stderr)
        return exc.code
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
