import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "fitness"))

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
    from insight_sources import coros_health
    monkeypatch.setattr(coros_health, "collect", boom)
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


# --- calendar load source ---------------------------------------------------

import calendar_load  # noqa: E402


def _ev(title, start, end, cal="Family", all_day=False):
    return {"title": title, "calendar": cal, "start": start, "end": end, "all_day": all_day}


def test_calendar_load_metrics_and_filters():
    events = [
        _ev("Dentist", "2026-10-13T09:00:00", "2026-10-13T10:00:00"),
        _ev("Overlap", "2026-10-13T09:30:00", "2026-10-13T11:00:00"),
        _ev("Dance class", "2026-10-13T17:00:00", "2026-10-13T18:00:00"),
        _ev("Caroline Work", "2026-10-13", "2026-10-14", all_day=True),
        _ev("Tik Tok Bowls", "2026-10-13", "2026-10-14", cal="Meal Planning", all_day=True),
        _ev("Columbus Day", "2026-10-13", "2026-10-14", cal="Holidays in United States", all_day=True),
        _ev("Flight to Aspen", "2026-10-08T07:00:00", "2026-10-08T09:00:00"),
    ]
    out = calendar_load.compute(events, date(2026, 10, 8), date(2026, 10, 14))
    d = out["2026-10-13"]
    assert d["events"] == 4 and d["hours_booked"] == 3.0  # 9-11 merged + 1h dance
    assert d["caroline_work"] == 1 and d["kid_activity"] == 1 and d["travel"] == 0
    assert out["2026-10-14"]["events"] == 0  # all-day end is exclusive
    assert out["2026-10-08"]["travel"] == 1
    assert out["2026-10-09"] == {"events": 0, "caroline_work": 0, "travel": 0, "kid_activity": 0, "hours_booked": 0.0}


def test_calendar_load_sync_cli_and_source(db, tmp_path, monkeypatch):
    monkeypatch.setattr(dayrating, "DB_PATH", db)
    f = tmp_path / "ev.json"
    f.write_text(json.dumps([_ev("Dance", "2026-10-13T17:00:00", "2026-10-13T18:00:00")]))
    assert dayrating_store.main(["calendar", "sync", "--start", "2026-10-12", "--end", "2026-10-14",
                                 "--events-file", str(f)]) == 0
    assert calendar_load.status(db_path=db)["days"] == 3
    insights.refresh(only="calendar_load", days=5, db_path=db, today=TODAY)
    conn = insights.connect(db)
    vals = dict(conn.execute("SELECT day, value FROM insight_daily WHERE metric='calendar:events'").fetchall())
    assert vals == {"2026-10-12": 0.0, "2026-10-13": 1.0, "2026-10-14": 0.0}
    assert dayrating_store.main(["calendar", "sync", "--start", "2026-10-14", "--end", "2026-10-12",
                                 "--events-file", str(f)]) == 2


SLEEP = ("Sleep Overview\n====\n\n2026-10-08\nSleep Score: 67\nDaily Sleep: 5h 48min (incl. naps)\n"
         "Main Sleep (asleep): 5h 48min\nDeep Sleep Ratio: 21%\nLight Sleep Ratio: 62%\nREM Ratio: 12%\n"
         "Awake Time: 20 min\nAwake Count (>5 min): 0\nMain Sleep Window: 2026-10-07 23:07 - 2026-10-08 05:15\n"
         "Naps Total: 0 min\n\n2026-10-09\nSleep Score: 84\nMain Sleep (asleep): 9h 0min\n"
         "Main Sleep Window: 2026-10-09 00:30 - 2026-10-09 07:30\n")
HEALTH = ("Daily Health Data - Last 2 days\n\n--- 20261013 ---\nSteps: 6,990 | Calories: 433 kcal | Exercise: 0 min\n"
          "Stress: Avg 26\nSleep Summary:\n  Sleep HR: Avg 51 bpm | Min 43 bpm\n\n--- 20261014 ---\n"
          "Steps: 493 | Calories: 61 kcal | Exercise: 0 min\nStress: Avg 20\n")
STRESS = "Stress Level\n\n2026-10-13:\nAverage Stress: 27 (Low)\n"


def test_coros_health_parsing_and_metrics():
    from insight_sources import coros_health
    out = coros_health.to_metrics(SLEEP, HEALTH, STRESS)
    assert out["coros:sleep_hours"]["2026-10-08"] == 5.8 and out["coros:sleep_score"]["2026-10-09"] == 84.0
    assert out["coros:deep_pct"]["2026-10-08"] == 21.0 and out["coros:wakeups"]["2026-10-08"] == 0.0
    assert out["coros:bedtime_after_noon"]["2026-10-08"] == pytest.approx(11 + 7 / 60)
    assert out["coros:bedtime_after_noon"]["2026-10-09"] == 12.5  # a past-midnight bedtime sorts after 23:07
    assert out["coros:wake_hour"]["2026-10-08"] == 5.25
    assert out["coros:steps"]["2026-10-13"] == 6990.0 and out["coros:sleep_hr_avg"]["2026-10-13"] == 51.0
    assert out["coros:stress_avg"]["2026-10-13"] == 27.0  # the dedicated stress report wins


def test_coros_health_collect_drops_partial_today(monkeypatch):
    import coros_mcp
    from insight_sources import coros_health
    monkeypatch.setattr(coros_mcp, "call_tools", lambda calls: [SLEEP, HEALTH, STRESS])
    out = coros_health.collect(None, TODAY - timedelta(days=7), TODAY, TODAY)
    assert "2026-10-14" not in out["coros:steps"] and "2026-10-13" in out["coros:steps"]


def test_coros_mcp_needs_sign_in(tmp_path, monkeypatch):
    import coros_mcp
    monkeypatch.setattr(coros_mcp, "TOKEN_FILE", tmp_path / "none.json")
    with pytest.raises(coros_mcp.CorosMcpError) as e:
        coros_mcp.access_token()
    assert e.value.code == 1
