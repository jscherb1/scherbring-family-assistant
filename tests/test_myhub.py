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
