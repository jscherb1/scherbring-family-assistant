import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dayrating  # noqa: E402
import habits  # noqa: E402
import myhub  # noqa: E402
import myhub_store  # noqa: E402

TODAY = date(2026, 10, 14)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "t.db"


def hub(db):
    return myhub.get_hub(db_path=db, today=TODAY)


def test_every_required_provider_is_registered():
    names = {m.PROVIDER["name"] for m in myhub.discover_providers()}
    assert set(myhub.REQUIRED_PROVIDERS) <= names


def test_empty_database_still_builds_every_card(db):
    h = hub(db)
    assert h["errors"] == {}
    assert set(myhub.REQUIRED_PROVIDERS) <= set(h["cards"])
    # nothing rated and nothing synced yet: both are surfaced, not hidden
    texts = " ".join(a["text"] for a in h["attention"])
    assert "isn't rated" in texts and "not synced" in texts


def test_attention_is_ranked_urgent_first(db):
    myhub.set_snapshot("task", [{"title": "Pay bill", "day": "2026-10-10", "priority": 1}], db_path=db)
    levels = [a["level"] for a in hub(db)["attention"]]
    assert levels == sorted(levels, key=lambda lv: myhub.LEVELS[lv])
    # tasks are grouped under "Show all > Tasks", so the overdue one is in groups, ranked first there
    assert "Pay bill" in hub(db)["groups"][0]["items"][0]["text"]


def test_broken_provider_becomes_visible_attention(db, monkeypatch):
    mod = next(m for m in myhub.discover_providers() if m.PROVIDER["name"] == "weather")
    monkeypatch.setattr(mod, "collect", lambda conn, today: 1 / 0)
    h = hub(db)
    assert "weather" in h["errors"]
    assert any("couldn't be read" in a["text"] for a in h["attention"])


def test_habits_and_mood_flow_in_from_services(db):
    h1 = habits.create_habit("Pushups", "daily", 1, db_path=db)
    habits.log_completion(h1["id"], log_date="2026-10-13", db_path=db, today=TODAY)
    dayrating.set_rating(8, "good", rating_date="2026-10-14", db_path=db, today=TODAY)
    h = hub(db)
    assert h["cards"]["mood"]["today"]["score"] == 8
    assert h["cards"]["habits"]["goal"][0]["name"] == "Pushups"
    assert not any("isn't rated" in a["text"] for a in h["attention"] if a["text"].startswith("Today"))


def test_snapshot_replaces_range_and_validates(db):
    ev = [{"title": "Dance", "day": "2026-10-14", "start_time": "17:00", "source": "Kids"},
          {"title": "Dinner out", "day": "2026-10-15", "source": "Meal Planning"}]
    myhub.set_snapshot("event", ev, start="2026-10-14", end="2026-10-21", db_path=db)
    myhub.set_snapshot("event", ev[:1], start="2026-10-14", end="2026-10-21", db_path=db)
    assert myhub.snapshot_status(db_path=db)["event"]["count"] == 1
    for bad in ([{"title": "x", "day": "nope"}], [{"title": "", "day": "2026-10-14"}],
                [{"title": "x", "day": "2026-10-14", "priority": 9}]):
        with pytest.raises(myhub.HubError):
            myhub.set_snapshot("task", bad, db_path=db)
    with pytest.raises(myhub.HubError):
        myhub.set_snapshot("event", ev, db_path=db)  # no range
    assert myhub.snapshot_status(db_path=db)["event"]["count"] == 1  # failed writes changed nothing


def test_schedule_card_splits_today_dinner_and_tasks(db):
    myhub.set_snapshot("event", [
        {"title": "Dance", "day": "2026-10-14", "start_time": "17:00", "source": "Kids"},
        {"title": "Tik Tok Bowls", "day": "2026-10-14", "source": "Meal Planning"},
        {"title": "Dentist", "day": "2026-10-16", "start_time": "09:00", "source": "Family"}],
        start="2026-10-14", end="2026-10-21", db_path=db)
    myhub.set_snapshot("task", [{"title": "Call", "day": "2026-10-14"}, {"title": "Old", "day": "2026-10-12"}], db_path=db)
    s = hub(db)["cards"]["schedule"]
    assert [e["title"] for e in s["today"]] == ["Dance"]
    assert s["dinner"]["title"] == "Tik Tok Bowls"
    assert [e["title"] for e in s["upcoming"]] == ["Dentist"]
    assert [t["title"] for t in s["tasks"]["overdue"]] == ["Old"]


def test_weather_alert_and_expired_login_are_flagged(db, tmp_path):
    conn = myhub.connect(db)
    conn.execute("INSERT INTO weather_alerts (id, alert_type, target_date, created_at) VALUES ('a','rain_cushions','2026-10-15','x')")
    conn.commit()
    conn.close()
    import system_health
    system_health.record("hyvee", "down", "login expired", db_path=db)
    texts = [a["text"] for a in hub(db)["attention"]]
    assert any("Rain coming" in t for t in texts) and any("Hy-Vee is down" in t for t in texts)


def test_cli_summary_and_snapshot(db, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(myhub, "connect", lambda db_path=None: dayrating.connect(db))
    f = tmp_path / "t.json"
    f.write_text(json.dumps([{"title": "A", "day": "2026-10-14"}]))
    assert myhub_store.main(["snapshot", "set", "--kind", "task", "--items-file", str(f)]) == 0
    capsys.readouterr()
    assert myhub_store.main(["attention"]) == 0
    assert "counts" in json.loads(capsys.readouterr().out)
    f.write_text("not json")
    assert myhub_store.main(["snapshot", "set", "--kind", "task", "--items-file", str(f)]) == 2


def test_tasks_are_grouped_out_of_the_attention_strip(db):
    myhub.set_snapshot("task", [{"title": "Call", "day": "2026-10-14"}, {"title": "Old", "day": "2026-10-12"}], db_path=db)
    h = hub(db)
    assert not any(a["key"].startswith("task") for a in h["attention"])
    assert [g["label"] for g in h["groups"]] == ["Tasks"]
    assert len(h["groups"][0]["items"]) == 2
    assert h["counts"]["urgent"] == sum(1 for a in h["attention"] if a["level"] == "urgent")


# ---- notifications ----

def _overdue(db, *titles):
    myhub.set_snapshot("task", [{"title": t, "day": "2026-10-10", "priority": 1} for t in titles], db_path=db)


def nc(db, hour, day=14, **kw):
    return myhub.notify_check(db_path=db, now=datetime(2026, 10, day, hour, 0), **kw)


def test_new_item_is_announced_once_then_quiet(db):
    myhub.notify_check(db_path=db, now=datetime(2026, 10, 14, 7, 0))  # morning digest used up
    _overdue(db, "Pay bill")
    first = nc(db, 9)
    assert first["send"] and "Pay bill" in first["message"] and first["mode"] == "update"
    again = nc(db, 11)
    assert not again["send"]  # same item, only 2h later


def test_resyncing_the_snapshot_does_not_make_items_new_again(db):
    nc(db, 7)
    _overdue(db, "Pay bill")
    assert nc(db, 9)["send"]
    _overdue(db, "Pay bill")  # a refresh replaces every task row with a new id
    assert not nc(db, 11)["send"]


def test_urgent_repeats_every_six_hours_while_awake(db):
    nc(db, 7)
    _overdue(db, "Pay bill")
    assert nc(db, 8)["send"]
    assert not nc(db, 13)["send"]
    r = nc(db, 14)
    assert r["send"] and r["reminders"] and "Still urgent" in r["message"]


def test_quiet_window_only_lets_new_urgent_through(db):
    nc(db, 7)  # consume the day's digest
    myhub.set_snapshot("task", [{"title": "Call vet", "day": "2026-10-14", "priority": 1}], db_path=db)  # warn
    assert not nc(db, 21)["send"]
    myhub.set_snapshot("task", [{"title": "Call vet", "day": "2026-10-14", "priority": 1},
                                {"title": "Pay bill", "day": "2026-10-10", "priority": 1}], db_path=db)
    r = nc(db, 22)
    assert r["send"] and "Pay bill" in r["message"] and "Call vet" not in r["message"]
    # the warn item was held back, not lost: it is in the next morning's digest
    d = nc(db, 7, day=14 + 1)
    assert d["mode"] == "digest" and "Call vet" in d["message"] and "Pay bill" in d["message"]


def test_morning_digest_once_per_day_and_covers_open_items(db):
    _overdue(db, "Pay bill")
    d = nc(db, 7)
    assert d["mode"] == "digest" and "Pay bill" in d["message"]
    assert not nc(db, 9)["send"]
    assert nc(db, 7, day=15)["mode"] == "digest"  # next day, repeats by design
    assert nc(db, 9, day=15)["send"] is False


def test_same_key_escalation(db, monkeypatch):
    mod = next(m for m in myhub.discover_providers() if m.PROVIDER["name"] == "weather")
    level = {"v": "warn"}
    monkeypatch.setattr(mod, "collect", lambda conn, today: {"card": {}, "attention": [
        {"key": "weather:x", "text": "Storm", "level": level["v"]}]})
    nc(db, 7)
    assert nc(db, 9)["send"] is False  # the 7am digest already covered it
    level["v"] = "urgent"
    r = nc(db, 10)
    assert r["send"] and r["escalated"] == ["weather:x"]


def test_dismissed_item_never_notifies_and_is_forgotten(db):
    nc(db, 7)
    _overdue(db, "Pay bill")
    key = next(a["key"] for g in hub(db)["groups"] for a in g["items"])
    myhub.dismiss(key, db_path=db, today=TODAY)
    assert not nc(db, 9)["send"]
    assert not nc(db, 15)["send"]
    myhub.restore(key, db_path=db)
    assert nc(db, 16)["send"]  # restored: counts as new


def test_dismiss_key_works_without_hub_membership_and_survives_hub_dismiss(db):
    myhub.dismiss_key("sched:foo:1", "A job failed", db_path=db)
    _overdue(db, "Pay bill")
    key = next(a["key"] for g in hub(db)["groups"] for a in g["items"])
    myhub.dismiss(key, db_path=db, today=TODAY)
    assert set(myhub.dismissed_rows(db_path=db)) == {"sched:foo:1", key}


def test_dry_run_does_not_record(db):
    nc(db, 7)
    _overdue(db, "Pay bill")
    assert nc(db, 9, record=False)["send"]
    assert nc(db, 9)["send"]


def test_notify_check_cli(db, monkeypatch, capsys):
    monkeypatch.setattr(myhub, "connect", lambda p=None: dayrating.connect(db))
    assert myhub_store.main(["notify-check", "--dry-run"]) == 0
    assert "send" in json.loads(capsys.readouterr().out)


# ---- stale snapshots ----

def _age_tasks(db, stamp):
    conn = myhub.connect(db)
    conn.execute("UPDATE hub_items SET synced_at=? WHERE kind='task'", (stamp,))
    conn.commit()
    conn.close()


def test_stale_task_snapshot_adds_caveat_but_keeps_the_items(db):
    myhub.notify_check(db_path=db, now=datetime(2026, 10, 14, 7, 0))  # digest used up
    _overdue(db, "Pay bill")
    _age_tasks(db, "2026-10-14T01:00:00")
    r = nc(db, 9)
    assert r["send"] and "Pay bill" in r["message"]
    assert "Could not refresh: Tasks (last refreshed 8h ago)" in r["message"]


def test_stale_snapshot_alerts_alone_then_repeats_every_six_hours(db):
    myhub.notify_check(db_path=db, now=datetime(2026, 10, 14, 7, 0))
    _overdue(db, "Pay bill")
    nc(db, 8)  # announce Pay bill while fresh
    _age_tasks(db, "2026-10-14T01:00:00")
    first = nc(db, 10)
    assert first["send"] and first["mode"] == "alert" and "Pay bill" not in first["message"]
    assert not nc(db, 12)["send"]  # only 2h later
    assert nc(db, 16)["send"]


def test_stale_alert_waits_for_awake_hours_and_resets_when_fresh(db):
    _overdue(db, "Pay bill")
    nc(db, 9)
    _age_tasks(db, "2026-10-14T01:00:00")
    assert not nc(db, 22)["send"]  # quiet window
    myhub.set_snapshot("task", [], db_path=db)  # a good refresh lands (stamped with the real clock)
    assert not nc(db, 11)["send"]
