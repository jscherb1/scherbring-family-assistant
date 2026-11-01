"""Scripted hub refresh: mapping, local-day boundaries, failure handling, dry run, notify guard."""
import json
from datetime import date, datetime

import pytest

import ha
import hub_refresh as hr
import myhub
import notify
import todoist_api as ta

TODAY = date(2026, 11, 3)  # Chicago is on CST (UTC-6) by then
NOW = datetime(2026, 11, 3, 10, 0)

CALS = [{"name": "Scherbring family", "entity_id": "calendar.scherbring_family"},
        {"name": "Meal planning", "entity_id": "calendar.meal_planning"}]


@pytest.fixture
def db(tmp_path):
    return tmp_path / "hub.db"


def stub_ha(monkeypatch, events=None, fail=(), cals=CALS, queries=None):
    events = events or {}

    def fake(path, body=None, method=None, raw=False):
        if path == "/api/calendars":
            return cals
        eid = path.split("/api/calendars/")[1].split("?")[0]
        if queries is not None:
            queries.append(path)
        if eid in fail:
            raise ha.HAError("Home Assistant returned HTTP 500", 1)
        return events.get(eid, [])
    monkeypatch.setattr(ha, "_open", fake)


def stub_todoist(monkeypatch, projects=None, tasks=None, error=None):
    def projs():
        if error:
            raise error
        return projects or []

    def open_tasks(project_id=None):
        if error:
            raise error
        return tasks or []
    monkeypatch.setattr(ta, "list_projects", projs)
    monkeypatch.setattr(ta, "list_open_tasks", open_tasks)


def rows(db, kind):
    c = myhub.connect(db)
    try:
        return [dict(r) for r in c.execute("SELECT * FROM hub_items WHERE kind=? ORDER BY day, title", (kind,))]
    finally:
        c.close()


def test_evening_event_stays_on_its_local_day_and_window_uses_local_midnight(monkeypatch, db):
    queries = []
    # 8:30pm CST on Nov 3 is 02:30Z Nov 4: a UTC-midnight window would file it on the wrong day
    ev = {"calendar.scherbring_family": [{"summary": "Concert", "start": {"dateTime": "2026-11-03T20:30:00-06:00"},
                                         "end": {"dateTime": "2026-11-03T22:00:00-06:00"}}]}
    stub_ha(monkeypatch, ev, queries=queries)
    stub_todoist(monkeypatch)
    res = hr.refresh(db_path=db, today=TODAY, kinds=("event",))
    assert res["event"]["status"] == "updated"
    [r] = rows(db, "event")
    assert (r["day"], r["start_time"], r["end_time"], r["source"]) == ("2026-11-03", "20:30", "22:00", "Scherbring Family")
    assert "start=2026-11-03T06%3A00%3A00Z" in queries[0]          # local midnight, not UTC midnight
    assert "end=2026-11-11T06%3A00%3A00Z" in queries[0]            # today+7 inclusive


def test_event_without_offset_is_read_as_local(monkeypatch):
    ev = {"calendar.scherbring_family": [{"summary": "X", "start": {"dateTime": "2026-11-03T23:00:00"},
                                         "end": {"dateTime": "2026-11-04T00:30:00"}}]}
    stub_ha(monkeypatch, ev)
    items = ha.calendar_window("calendar.scherbring_family", TODAY, date(2026, 11, 10))
    assert items[0]["day"] == "2026-11-03" and items[0]["start_time"] == "23:00" and items[0]["end_time"] == "00:30"


def test_multi_day_all_day_event_appears_on_each_day_clipped(monkeypatch):
    ev = {"calendar.scherbring_family": [
        {"summary": "Trip", "start": {"date": "2026-11-05"}, "end": {"date": "2026-11-08"}},       # 5,6,7
        {"summary": "Long", "start": {"date": "2026-11-09"}, "end": {"date": "2026-11-20"}},       # clipped at 10
        {"summary": "Earlier", "start": {"date": "2026-10-30"}, "end": {"date": "2026-11-04"}}]}   # 3 only
    stub_ha(monkeypatch, ev)
    items = ha.calendar_window("calendar.scherbring_family", TODAY, date(2026, 11, 10))
    by = {}
    for i in items:
        by.setdefault(i["title"], []).append(i["day"])
        assert i["start_time"] is None and i["all_day"]
    assert by["Trip"] == ["2026-11-05", "2026-11-06", "2026-11-07"]
    assert by["Long"] == ["2026-11-09", "2026-11-10"]
    assert by["Earlier"] == ["2026-11-03"]


def test_meal_source_keeps_meal_prefix_and_holiday_calendars_skipped(monkeypatch, db):
    cals = CALS + [{"name": "US Holidays", "entity_id": "calendar.holidays"}]
    ev = {"calendar.meal_planning": [{"summary": "Tacos", "start": {"date": "2026-11-03"}, "end": {"date": "2026-11-04"}}],
          "calendar.holidays": [{"summary": "Day", "start": {"date": "2026-11-03"}, "end": {"date": "2026-11-04"}}]}
    stub_ha(monkeypatch, ev, cals=cals)
    stub_todoist(monkeypatch)
    hr.refresh(db_path=db, today=TODAY, kinds=("event",))
    [r] = rows(db, "event")
    assert r["source"] == "Meal Planning" and r["source"].lower().startswith("meal")


def seed_events(db, n=3):
    myhub.set_snapshot("event", [{"title": f"old{i}", "day": "2026-11-04", "source": "Scherbring Family"}
                                 for i in range(n)], start="2026-11-03", end="2026-11-10", db_path=db)


def test_one_failed_calendar_keeps_the_old_event_snapshot(monkeypatch, db):
    seed_events(db)
    ev = {"calendar.scherbring_family": [{"summary": "New", "start": {"date": "2026-11-04"}, "end": {"date": "2026-11-05"}}]}
    stub_ha(monkeypatch, ev, fail={"calendar.meal_planning"})
    stub_todoist(monkeypatch)
    res = hr.refresh(db_path=db, today=TODAY, kinds=("event",))
    assert res["event"]["status"] == "kept old snapshot" and res["ok"] is False
    assert "Meal planning" in res["event"]["errors"][0]
    assert [r["title"] for r in rows(db, "event")] == ["old0", "old1", "old2"]


def test_calendar_list_failure_keeps_old_snapshot(monkeypatch, db):
    seed_events(db)

    def boom(*a, **k):
        raise ha.HAError("Can't reach Home Assistant", 1)
    monkeypatch.setattr(ha, "_open", boom)
    res = hr.refresh(db_path=db, today=TODAY, kinds=("event",))
    assert res["event"]["status"] == "kept old snapshot"
    assert len(rows(db, "event")) == 3


def test_todoist_failure_keeps_old_tasks_but_events_still_update(monkeypatch, db):
    myhub.set_snapshot("task", [{"title": "keep me", "day": "2026-11-03", "priority": 4}], db_path=db)
    stub_ha(monkeypatch, {"calendar.scherbring_family": [{"summary": "E", "start": {"date": "2026-11-04"}, "end": {"date": "2026-11-05"}}]})
    stub_todoist(monkeypatch, error=ta.TodoistError("rejected", "auth"))
    res = hr.refresh(db_path=db, today=TODAY)
    assert res["task"]["status"] == "kept old snapshot" and "auth" in res["task"]["errors"][0]
    assert res["event"]["status"] == "updated"
    assert [r["title"] for r in rows(db, "task")] == ["keep me"]
    health = {r["key"]: r["status"] for r in myhub.connect(db).execute("SELECT key, status FROM system_health")}
    assert health["todoist_api"] == "warn" and health["hub_calendar"] == "ok"


def test_tasks_exclude_shopping_skip_undated_and_map_priority(monkeypatch, db):
    projects = [{"id": "1", "name": "Shopping"}, {"id": "2", "name": "Costco"}, {"id": "3", "name": "Staple Groceries"},
                {"id": "4", "name": "Home"}]

    def t(c, p, due, prio=1):
        return {"content": c, "project_id": p, "priority": prio, "due": {"date": due} if due else None}
    tasks = [t("milk", "1", "2026-11-03"), t("bulk", "2", "2026-11-03"), t("staple", "3", "2026-11-03"),
             t("undated", "4", None), t("overdue", "4", "2026-10-30", 4), t("later", "4", "2027-01-04T09:00:00", 2)]
    stub_ha(monkeypatch)
    stub_todoist(monkeypatch, projects, tasks)
    hr.refresh(db_path=db, today=TODAY, kinds=("task",))
    got = {r["title"]: r for r in rows(db, "task")}
    assert set(got) == {"overdue", "later"}
    assert got["overdue"]["priority"] == 1 and got["later"]["priority"] == 3   # Todoist p1 = 1
    assert got["overdue"]["source"] == "Home" and got["later"]["day"] == "2027-01-04"


def test_dry_run_writes_nothing_and_reports_diff(monkeypatch, db):
    seed_events(db, 2)
    stub_ha(monkeypatch, {"calendar.scherbring_family": [{"summary": "N", "start": {"date": "2026-11-04"}, "end": {"date": "2026-11-05"}}]})
    stub_todoist(monkeypatch, [{"id": "4", "name": "Home"}],
                 [{"content": "t", "project_id": "4", "priority": 1, "due": {"date": "2026-11-03"}}])
    sent = []
    monkeypatch.setattr(notify, "send", lambda *a, **k: sent.append(1))
    before = (rows(db, "event"), rows(db, "task"))
    res = hr.refresh(db_path=db, today=TODAY, notify=True, dry_run=True)
    assert (rows(db, "event"), rows(db, "task")) == before
    assert res["event"]["added"] == 1 and res["event"]["removed"] == 2 and res["event"]["stored_now"] == 2
    assert res["task"]["added"] == 1 and res["task"]["fetched"] == 1
    assert res["notify"]["skipped"] == "dry run" and not sent
    c = myhub.connect(db)
    assert c.execute("SELECT COUNT(*) FROM system_health").fetchone()[0] == 0


class Delivered:
    def __init__(self, ok=True):
        self.delivered, self.error = ok, "" if ok else "boom"


def overdue_setup(monkeypatch):
    stub_ha(monkeypatch)
    stub_todoist(monkeypatch, [{"id": "4", "name": "Home"}],
                 [{"content": "pay bill", "project_id": "4", "priority": 1, "due": {"date": "2026-10-30"}}])


def add_agent_task(db, enabled):
    c = myhub.connect(db)
    c.execute("INSERT INTO scheduled_tasks (id,name,prompt,cron_expression,target_chat_id,enabled,created_at) "
              "VALUES ('x',?,?,?,?,?,?)", (hr.AGENT_TASK_NAME, "p", "0 * * * *", "1", int(enabled), "2026-01-01T00:00:00Z"))
    c.commit()
    c.close()


def test_notify_held_while_agent_task_enabled(monkeypatch, db):
    overdue_setup(monkeypatch)
    add_agent_task(db, True)
    sent = []
    monkeypatch.setattr(notify, "send", lambda text, **k: sent.append((text, k)) or Delivered())
    res = hr.refresh(db_path=db, today=TODAY, now=NOW, notify=True)
    assert not sent and "still enabled" in res["notify"]["skipped"]
    # nothing was recorded as told, so the agent path would still announce it
    assert myhub.connect(db).execute("SELECT COUNT(*) FROM hub_notified").fetchone()[0] == 0


def test_notify_sends_once_after_agent_task_disabled_and_dedupes(monkeypatch, db):
    overdue_setup(monkeypatch)
    add_agent_task(db, False)
    sent = []
    monkeypatch.setattr(notify, "send", lambda text, **k: sent.append((text, k)) or Delivered())
    res = hr.refresh(db_path=db, today=TODAY, now=NOW, notify=True)
    assert res["notify"]["sent"] and len(sent) == 1 and "pay bill" in sent[0][0]
    assert sent[0][1]["source"] == "hub-refresh"
    res2 = hr.refresh(db_path=db, today=TODAY, now=NOW, notify=True)   # same dedupe state: nothing new
    assert not res2["notify"]["sent"] and len(sent) == 1


def test_notify_not_attempted_without_flag(monkeypatch, db):
    overdue_setup(monkeypatch)
    sent = []
    monkeypatch.setattr(notify, "send", lambda *a, **k: sent.append(1))
    res = hr.refresh(db_path=db, today=TODAY, now=NOW)
    assert "notify" not in res and not sent


def test_failed_delivery_is_not_recorded_so_it_retries(monkeypatch, db):
    overdue_setup(monkeypatch)
    outcomes = [Delivered(False), Delivered(True)]
    sent = []
    monkeypatch.setattr(notify, "send", lambda text, **k: sent.append(text) or outcomes[len(sent) - 1])
    r1 = hr.refresh(db_path=db, today=TODAY, now=NOW, notify=True)
    assert r1["notify"]["sent"] is False and "error" in r1["notify"]
    r2 = hr.refresh(db_path=db, today=TODAY, now=NOW, notify=True)
    assert r2["notify"]["sent"] is True and len(sent) == 2


def seed_tasks(db, n=12):
    myhub.set_snapshot("task", [{"title": f"t{i}", "day": "2026-10-30", "priority": 4} for i in range(n)], db_path=db)


def test_empty_task_read_is_refused_and_does_not_reannounce(monkeypatch, db):
    add_agent_task(db, False)
    sent = []
    monkeypatch.setattr(notify, "send", lambda text, **k: sent.append(text) or Delivered())
    stub_ha(monkeypatch)
    twelve = [{"content": f"late{i}", "project_id": "4", "priority": 1, "due": {"date": "2026-10-30"}} for i in range(12)]
    projects = [{"id": "4", "name": "Home"}]
    stub_todoist(monkeypatch, projects, twelve)
    hr.refresh(db_path=db, today=TODAY, now=NOW, kinds=("task",), notify=True)     # good read: digest goes out
    assert len(sent) == 1
    told = myhub.connect(db).execute("SELECT COUNT(*) FROM hub_notified").fetchone()[0]
    assert told >= 12
    stub_todoist(monkeypatch, projects, [])                                        # Todoist "succeeds" with nothing
    res = hr.refresh(db_path=db, today=TODAY, now=NOW, kinds=("task",), notify=True)
    assert res["task"]["status"] == "kept old snapshot" and "kept the old snapshot" in res["task"]["errors"][0]
    assert len(rows(db, "task")) == 12
    assert myhub.connect(db).execute("SELECT COUNT(*) FROM hub_notified").fetchone()[0] == told  # nothing forgotten
    health = {r["key"]: r["status"] for r in myhub.connect(db).execute("SELECT key, status FROM system_health")}
    assert health["todoist_api"] == "warn"
    stub_todoist(monkeypatch, projects, twelve)                                    # the next good read
    hr.refresh(db_path=db, today=TODAY, now=NOW, kinds=("task",), notify=True)
    assert len(sent) == 1                                                          # no re-announce flood


def test_sharp_drop_refused_but_small_change_accepted(monkeypatch, db):
    seed_tasks(db, 12)
    stub_ha(monkeypatch)

    def tasks(n):
        return [{"content": f"n{i}", "project_id": "4", "priority": 1, "due": {"date": "2026-11-03"}} for i in range(n)]
    stub_todoist(monkeypatch, [{"id": "4", "name": "Home"}], tasks(3))      # 3 < 40% of 12
    assert hr.refresh(db_path=db, today=TODAY, kinds=("task",))["task"]["status"] == "kept old snapshot"
    stub_todoist(monkeypatch, [{"id": "4", "name": "Home"}], tasks(9))      # ordinary change
    assert hr.refresh(db_path=db, today=TODAY, kinds=("task",))["task"]["status"] == "updated"
    assert len(rows(db, "task")) == 9


def test_small_stored_list_may_go_empty(monkeypatch, db):
    seed_tasks(db, 3)                                                       # below the guard threshold
    stub_ha(monkeypatch)
    stub_todoist(monkeypatch, [], [])
    assert hr.refresh(db_path=db, today=TODAY, kinds=("task",))["task"]["status"] == "updated"
    assert rows(db, "task") == []


def test_genuinely_empty_list_is_accepted_after_n_refusals(monkeypatch, db):
    seed_tasks(db, 12)
    stub_ha(monkeypatch)
    stub_todoist(monkeypatch, [], [])
    statuses = [hr.refresh(db_path=db, today=TODAY, kinds=("task",))["task"]["status"] for _ in range(hr.ACCEPT_AFTER)]
    assert statuses == ["kept old snapshot"] * (hr.ACCEPT_AFTER - 1) + ["updated"]
    assert rows(db, "task") == []


def test_force_accepts_an_empty_read_at_once(monkeypatch, db):
    seed_tasks(db, 12)
    stub_ha(monkeypatch)
    stub_todoist(monkeypatch, [], [])
    assert hr.refresh(db_path=db, today=TODAY, kinds=("task",), force=True)["task"]["status"] == "updated"
    assert rows(db, "task") == []


def test_dry_run_does_not_advance_the_refusal_counter(monkeypatch, db):
    seed_tasks(db, 12)
    stub_ha(monkeypatch)
    stub_todoist(monkeypatch, [], [])
    for _ in range(hr.ACCEPT_AFTER + 2):
        assert hr.refresh(db_path=db, today=TODAY, kinds=("task",), dry_run=True)["task"]["status"] == "kept old snapshot"
    assert hr.meta_get("refresh_shrunk_task", db) is None


def test_empty_event_read_refused_when_many_stored(monkeypatch, db):
    seed_events(db, 8)
    stub_ha(monkeypatch)                                                    # every calendar "succeeds" empty
    res = hr.refresh(db_path=db, today=TODAY, kinds=("event",))
    assert res["event"]["status"] == "kept old snapshot" and len(rows(db, "event")) == 8


def test_disappearing_calendar_keeps_snapshot_and_warns_until_forced(monkeypatch, db):
    ev = {"calendar.scherbring_family": [{"summary": "E", "start": {"date": "2026-11-04"}, "end": {"date": "2026-11-05"}}]}
    stub_ha(monkeypatch, ev)
    assert hr.refresh(db_path=db, today=TODAY, kinds=("event",))["event"]["status"] == "updated"
    assert hr.meta_get("refresh_calendars", db) == ["calendar.meal_planning", "calendar.scherbring_family"]
    stub_ha(monkeypatch, ev, cals=CALS[:1])                                 # Meal planning vanished from HA
    res = hr.refresh(db_path=db, today=TODAY, kinds=("event",))
    assert res["event"]["status"] == "kept old snapshot" and "no longer listed" in res["event"]["errors"][0]
    health = {r["key"]: r["status"] for r in myhub.connect(db).execute("SELECT key, status FROM system_health")}
    assert health["hub_calendar"] == "warn"
    assert hr.refresh(db_path=db, today=TODAY, kinds=("event",), force=True)["event"]["status"] == "updated"
    assert hr.meta_get("refresh_calendars", db) == ["calendar.scherbring_family"]


def test_new_calendar_is_not_an_error(monkeypatch, db):
    stub_ha(monkeypatch, cals=CALS[:1])
    assert hr.refresh(db_path=db, today=TODAY, kinds=("event",))["event"]["status"] == "updated"
    stub_ha(monkeypatch, cals=CALS)
    assert hr.refresh(db_path=db, today=TODAY, kinds=("event",))["event"]["status"] == "updated"


def test_birthdays_calendar_is_excluded_to_keep_the_old_set(monkeypatch, db):
    cals = CALS + [{"name": "Birthdays", "entity_id": "calendar.birthdays"}]
    ev = {"calendar.birthdays": [{"summary": "B", "start": {"date": "2026-11-03"}, "end": {"date": "2026-11-04"}}]}
    stub_ha(monkeypatch, ev, cals=cals)
    hr.refresh(db_path=db, today=TODAY, kinds=("event",))
    assert rows(db, "event") == []


def test_malformed_event_does_not_kill_the_run_or_skip_tasks_and_health(monkeypatch, db):
    seed_events(db, 3)
    bad = {"calendar.scherbring_family": [{"summary": "Bad", "start": {"date": "not-a-date"}, "end": {"date": "x"}}],
           "calendar.meal_planning": [{"summary": "NoStart"}]}
    stub_ha(monkeypatch, bad)
    stub_todoist(monkeypatch, [{"id": "4", "name": "Home"}],
                 [{"content": "ok task", "project_id": "4", "priority": 1, "due": {"date": "2026-11-03"}}])
    res = hr.refresh(db_path=db, today=TODAY)
    assert res["event"]["status"] == "kept old snapshot" and len(res["event"]["errors"]) >= 1
    assert res["task"]["status"] == "updated" and [r["title"] for r in rows(db, "task")] == ["ok task"]
    assert len(rows(db, "event")) == 3
    health = {r["key"]: r["status"] for r in myhub.connect(db).execute("SELECT key, status FROM system_health")}
    assert health["hub_calendar"] == "warn" and health["todoist_api"] == "ok"


def test_unexpected_crash_in_one_kind_still_runs_the_other(monkeypatch, db):
    def boom(*a, **k):
        raise KeyError("surprise")
    monkeypatch.setattr(hr, "fetch_events", boom)
    stub_todoist(monkeypatch, [{"id": "4", "name": "Home"}],
                 [{"content": "ok task", "project_id": "4", "priority": 1, "due": {"date": "2026-11-03"}}])
    res = hr.refresh(db_path=db, today=TODAY)
    assert "crashed" in res["event"]["errors"][0] and res["task"]["status"] == "updated"
    health = {r["key"]: r["status"] for r in myhub.connect(db).execute("SELECT key, status FROM system_health")}
    assert health["hub_calendar"] == "warn" and health["todoist_api"] == "ok"


def test_failed_send_records_a_health_warn_without_alerting(monkeypatch, db):
    overdue_setup(monkeypatch)
    alerts = []
    import system_health
    monkeypatch.setattr(system_health, "telegram_notify", lambda m: alerts.append(m))
    monkeypatch.setattr(notify, "send", lambda text, **k: Delivered(False))
    hr.refresh(db_path=db, today=TODAY, now=NOW, notify=True)
    health = {r["key"]: r["status"] for r in myhub.connect(db).execute("SELECT key, status FROM system_health")}
    assert health["hub_notify"] == "warn" and not alerts


def test_overnight_freshness_is_not_amber(monkeypatch, db):
    import insights
    monkeypatch.setattr(insights, "freshness", lambda db_path=None: {})
    myhub.set_snapshot("task", [{"title": "t", "day": "2026-11-03"}], db_path=db)
    c = myhub.connect(db)
    c.execute("UPDATE hub_items SET synced_at='2026-11-02T22:45:00'")
    c.commit()
    c.close()
    clock = {"at": datetime(2026, 11, 3, 3, 0)}

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["at"]
    monkeypatch.setattr(myhub, "datetime", FakeDT)
    assert {f["key"]: f["stale"] for f in myhub.freshness(db_path=db)}["tasks"] is False   # 3am, last run 10:45pm
    clock["at"] = datetime(2026, 11, 3, 14, 0)                                              # same age by day: stale
    assert {f["key"]: f["stale"] for f in myhub.freshness(db_path=db)}["tasks"] is True


def test_cli_dry_run_prints_json(monkeypatch, db, capsys):
    stub_ha(monkeypatch)
    stub_todoist(monkeypatch)
    assert hr.main(["--dry-run", "--db", str(db)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True and out["event"]["status"] == "dry-run"
