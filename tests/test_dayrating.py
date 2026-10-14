import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dayrating  # noqa: E402
import dayrating_store  # noqa: E402
import habits  # noqa: E402
import insights  # noqa: E402

TODAY = date(2026, 10, 14)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "t.db"


def rate(db, score, day, note=None):
    return dayrating.set_rating(score, note, rating_date=day.isoformat(), db_path=db, today=TODAY)


def test_rating_validation_and_upsert(db):
    for bad in (0, 11, "x", None):
        with pytest.raises(dayrating.RatingError):
            dayrating.set_rating(bad, db_path=db, today=TODAY)
    with pytest.raises(dayrating.RatingError):
        rate(db, 5, TODAY + timedelta(days=1))
    rate(db, 6, TODAY, "ok day")
    r = rate(db, 8, TODAY)  # edit keeps the note when none is given
    assert r["score"] == 8 and r["note"] == "ok day"
    assert len(dayrating.list_ratings(db_path=db, today=TODAY)) == 1


def test_status_streak_and_nudge(db):
    assert dayrating.needs_nudge(db_path=db, today=TODAY)["nudge"] is True
    for i in (1, 2, 3):
        rate(db, 7, TODAY - timedelta(days=i))
    s = dayrating.status(db_path=db, today=TODAY)
    assert s["logging_streak"] == 3 and not s["rated_today"] and s["yesterday_unrated"] is False
    rate(db, 9, TODAY)
    assert dayrating.needs_nudge(db_path=db, today=TODAY)["nudge"] is False
    assert dayrating.delete_rating(TODAY.isoformat(), db_path=db, today=TODAY)["deleted"]
    with pytest.raises(dayrating.RatingError) as e:
        dayrating.delete_rating(TODAY.isoformat(), db_path=db, today=TODAY)
    assert e.value.code == 1


def test_spearman_and_binary_analysis():
    assert insights.spearman([1, 2, 3, 4], [2, 4, 6, 8]) == pytest.approx(1.0)
    assert insights.spearman([1, 1, 1], [1, 2, 3]) is None
    assert insights.analyze_pair([1.0, 0.0] * 3, [5.0] * 6) is None  # too few days
    x = [1.0, 0.0] * 10
    y = [9.0 if v else 4.0 for v in x]
    res = insights.analyze_pair(x, y)
    assert res["kind"] == "yes_no" and res["effect"] == 5.0 and res["confidence"] in ("emerging", "solid")


def seed_workout_days(db, days=30):
    hid = habits.create_habit("Workouts", "weekly", 5, db_path=db)["id"]
    for i in range(days):
        day = TODAY - timedelta(days=i)
        worked = i % 2 == 0
        if worked:
            habits.log_completion(hid, log_date=day.isoformat(), db_path=db, today=TODAY)
        rate(db, 8 if worked else 4, day)


def test_workouts_found_as_driver_and_sources_flow_in(db):
    seed_workout_days(db)
    report = insights.refresh(only="habits", db_path=db, today=TODAY)
    assert report["habits"]["metrics"] >= 2
    out = insights.analyze(days=30, db_path=db, today=TODAY)
    top = out["findings"][0]
    assert top["metric"].startswith("habit:") and top["effect"] == 4.0
    assert out["enough_data"] and any(s["name"] == "habits" and s["metrics"] for s in out["sources"])
    assert insights.analyze(days=30, db_path=db, today=TODAY) == out  # deterministic


def test_new_source_module_is_auto_discovered(db, monkeypatch, tmp_path):
    import insight_sources
    (tmp_path / "zz_fake.py").write_text(
        "SOURCE={'name':'zz_fake','kind':'manual','description':'x'}\n"
        "def collect(conn,start,end,today):\n    return {'fake:m': {end.isoformat(): 2.0}}\n")
    monkeypatch.setattr(insight_sources, "__path__", list(insight_sources.__path__) + [str(tmp_path)])
    assert "zz_fake" in [m.SOURCE["name"] for m in insights.discover_sources()]
    insights.refresh(only="zz_fake", db_path=db, today=TODAY)
    assert insights.connect(db).execute("SELECT value FROM insight_daily WHERE metric='fake:m'").fetchone()[0] == 2.0


def test_failing_source_does_not_block_others(db, monkeypatch):
    from insight_sources import coros_wellness, weather

    def boom(*a):
        raise OSError("offline")
    monkeypatch.setattr(weather, "collect", boom)
    monkeypatch.setattr(coros_wellness, "collect", boom)  # never hit the network or the real COROS login in tests
    report = insights.refresh(days=3, db_path=db, today=TODAY)
    assert "error" in report["weather"] and report["habits"].get("error") is None


def test_coros_wellness_mapping():
    from insight_sources import coros_wellness
    out = coros_wellness.to_metrics([{"happenDay": 20261013, "avgSleepHrv": 50, "sleepHrvBase": 43, "rhr": 47,
                                      "tiredRateNew": -10, "trainingLoad": 0, "trainingLoadRatio": 0, "t7d": 30},
                                     {"happenDay": 20261014, "avgSleepHrv": None, "rhr": 0}])
    assert out["coros:hrv_ms"] == {"2026-10-13": 50.0} and out["coros:hrv_vs_baseline"]["2026-10-13"] == 7.0
    assert "2026-10-14" not in out.get("coros:resting_hr", {}) and out["coros:training_load"]["2026-10-13"] == 0.0


def test_save_and_latest_report_and_cli(db, monkeypatch, capsys):
    monkeypatch.setattr(dayrating, "DB_PATH", db)
    assert insights.latest_report(db_path=db) is None
    insights.save_report("Good weeks follow workouts.", days=30, db_path=db, today=TODAY)
    assert insights.latest_report(db_path=db)["narrative"].startswith("Good weeks")
    with pytest.raises(insights.InsightError):
        insights.save_report("  ", db_path=db)
    assert dayrating_store.main(["rate", "--score", "7", "--note", "fine"]) == 0
    assert json.loads(capsys.readouterr().out)["score"] == 7
    assert dayrating_store.main(["rate", "--score", "12"]) == 2
    assert dayrating_store.main(["delete", "--date", "2020-01-01"]) == 1
