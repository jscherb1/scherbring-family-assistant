import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "fitness"))

import habits  # noqa: E402
import workout_sync  # noqa: E402

TODAY = date(2026, 10, 14)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    habits.create_habit("Workouts", "weekly", 5, db_path=path)
    return path


def test_activity_days_groups_multiple_activities_per_day():
    acts = [{"date": 20261012, "name": "Run"}, {"date": 20261012, "name": "Peloton"},
            {"date": 20261009, "customName": "Sauna"}, {"date": "bad"}, {}]
    assert workout_sync.activity_days(acts) == {
        "2026-10-12": ["Run", "Peloton"], "2026-10-09": ["Sauna"]}


def test_sync_logs_one_per_day_and_is_idempotent(db):
    days = {"2026-10-12": ["Run", "Peloton"], "2026-10-13": ["Lift"]}
    first = workout_sync.sync(days, db_path=db, today=TODAY)
    assert first["logged"] == ["2026-10-12", "2026-10-13"]
    hid = habits.resolve_habit("Workouts", db)["id"]
    logs = habits.list_logs(hid, limit=None, db_path=db)
    assert len(logs) == 2 and all(l["count"] == 1 for l in logs)
    second = workout_sync.sync(days, db_path=db, today=TODAY)
    assert second["logged"] == [] and second["skipped"] == ["2026-10-12", "2026-10-13"]
    assert len(habits.list_logs(hid, limit=None, db_path=db)) == 2


def test_sync_skips_day_already_logged_by_hand(db):
    hid = habits.resolve_habit("Workouts", db)["id"]
    habits.log_completion(hid, log_date="2026-10-12", db_path=db, today=TODAY)
    res = workout_sync.sync({"2026-10-12": ["Run"]}, db_path=db, today=TODAY)
    assert res["logged"] == [] and len(habits.list_logs(hid, limit=None, db_path=db)) == 1


def test_dry_run_writes_nothing(db):
    res = workout_sync.sync({"2026-10-12": ["Run"]}, db_path=db, dry_run=True, today=TODAY)
    hid = habits.resolve_habit("Workouts", db)["id"]
    assert res["logged"] == ["2026-10-12"] and habits.list_logs(hid, db_path=db) == []


def test_missing_habit_raises(tmp_path):
    with pytest.raises(habits.HabitError):
        workout_sync.sync({"2026-10-12": ["Run"]}, db_path=tmp_path / "x.db", today=TODAY)


def test_parse_sport_records_groups_by_day():
    text = ("Sport Records — x\n====\n\n1. Strength — 2026-10-12\n   Location: 30 Min Upper Body - Peloton\n"
            "   Duration: 24:45\n\n2. Cycling — 2026-10-09\n   Location: Aspen Road Bike\n\n"
            "3. Cycling — 2026-10-09\n   Location: Pitkin\n\n4. Run — 2026-10-08\n   Duration: 1\n")
    assert workout_sync.parse_sport_records(text) == {
        "2026-10-12": ["30 Min Upper Body - Peloton"], "2026-10-09": ["Aspen Road Bike", "Pitkin"],
        "2026-10-08": ["Run"]}
