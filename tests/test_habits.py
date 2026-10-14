import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import habits  # noqa: E402
import habits_store  # noqa: E402

MON = date(2026, 10, 12)  # a Monday


@pytest.fixture
def db(tmp_path):
    return tmp_path / "t.db"


def daily(db, name="Stretch", target=1, **kw):
    return habits.create_habit(name, "daily", target, db_path=db, **kw)["id"]


def weekly(db, name="Run", target=3, **kw):
    return habits.create_habit(name, "weekly", target, db_path=db, **kw)["id"]


def log(db, hid, day, count=1):
    habits.log_completion(hid, log_date=day.isoformat(), count=count, db_path=db, today=day)


def status(db, hid, today):
    return habits.get_status(hid, db_path=db, today=today)


# ---- daily streaks ----

def test_daily_streak_counts_consecutive_days_including_today(db):
    hid = daily(db)
    today = MON
    for i in range(3):
        log(db, hid, today - timedelta(days=i))
    s = status(db, hid, today)
    assert s["current_streak"] == 3 and s["longest_streak"] == 3 and s["period_status"] == "met"


def test_unmet_today_does_not_break_streak_but_is_at_risk(db):
    hid = daily(db)
    for i in (1, 2, 3):
        log(db, hid, MON - timedelta(days=i))
    s = status(db, hid, MON)
    assert s["current_streak"] == 3 and s["period_status"] == "at_risk" and s["at_risk"]


def test_missed_day_resets_streak_and_keeps_longest(db):
    hid = daily(db)
    for i in (4, 5, 6):  # a 3-day run that ended 4 days ago
        log(db, hid, MON - timedelta(days=i))
    log(db, hid, MON)  # new streak restarts today
    s = status(db, hid, MON)
    assert s["current_streak"] == 1 and s["longest_streak"] == 3


def test_missed_yesterday_means_no_streak_and_no_risk(db):
    hid = daily(db)
    log(db, hid, MON - timedelta(days=3))
    s = status(db, hid, MON)
    assert s["current_streak"] == 0 and not s["at_risk"] and s["period_status"] == "open"


def test_daily_target_needs_all_counts(db):
    hid = daily(db, "Water", target=3)
    log(db, hid, MON, count=2)
    s = status(db, hid, MON)
    assert s["done_this_period"] == 2 and s["remaining"] == 1 and s["current_streak"] == 0
    log(db, hid, MON)
    assert status(db, hid, MON)["current_streak"] == 1


# ---- weekly streaks ----

def test_weekly_streak_and_week_boundary(db):
    hid = weekly(db, target=2)
    sun = MON - timedelta(days=1)  # belongs to the previous week
    last_mon = MON - timedelta(days=7)
    log(db, hid, last_mon)
    log(db, hid, sun)
    log(db, hid, MON)
    s = status(db, hid, MON + timedelta(days=1))  # Tuesday
    assert s["done_this_period"] == 1 and s["current_streak"] == 1 and s["period_start"] == MON.isoformat()
    log(db, hid, MON + timedelta(days=1))
    assert status(db, hid, MON + timedelta(days=1))["current_streak"] == 2


def test_weekly_at_risk_when_remaining_fits_days_left(db):
    hid = weekly(db, target=3)
    last_week = MON - timedelta(days=7)
    for i in range(3):
        log(db, hid, last_week + timedelta(days=i))
    log(db, hid, MON)  # 1 of 3 this week
    assert not status(db, hid, MON + timedelta(days=1))["at_risk"]  # Tue: 6 days left, 2 needed
    assert status(db, hid, MON + timedelta(days=4))["at_risk"]  # Fri: 3 days left, 2 needed
    assert status(db, hid, MON + timedelta(days=6))["at_risk"]  # Sun


def test_missed_week_resets_weekly_streak(db):
    hid = weekly(db, target=1)
    log(db, hid, MON - timedelta(days=21))
    log(db, hid, MON - timedelta(days=14))
    # nothing last week -> streak is broken once this week is also unmet
    s = status(db, hid, MON + timedelta(days=6))
    assert s["current_streak"] == 0 and s["longest_streak"] == 2


# ---- no-goal habits ----

def test_no_goal_habit_counts_without_streak(db):
    hid = habits.create_habit("Ate out", "weekly", None, db_path=db)["id"]
    log(db, hid, MON - timedelta(days=10))
    log(db, hid, MON - timedelta(days=4))
    log(db, hid, MON)
    s = status(db, hid, MON)
    assert s["has_goal"] is False and s["current_streak"] is None and not s["at_risk"]
    assert s["total_count"] == 3 and s["count_this_week"] == 1 and s["count_last_30_days"] == 3
    assert s["avg_gap_days"] == 5.0 and s["last_done"] == MON.isoformat()


# ---- logging rules ----

def test_backfill_allowed_future_rejected(db):
    hid = daily(db)
    log(db, hid, MON - timedelta(days=2))
    with pytest.raises(habits.HabitError):
        habits.log_completion(hid, log_date=(MON + timedelta(days=1)).isoformat(), db_path=db, today=MON)
    with pytest.raises(habits.HabitError):
        habits.log_completion(hid, log_date="nope", db_path=db, today=MON)


def test_undo_removes_most_recently_entered_log(db):
    hid = daily(db)
    log(db, hid, MON)
    log(db, hid, MON - timedelta(days=5))  # entered last, though dated earlier
    res = habits.undo_last(hid, db_path=db, today=MON)
    assert res["removed"]["log_date"] == (MON - timedelta(days=5)).isoformat()
    assert res["status"]["total_count"] == 1
    habits.undo_last(hid, db_path=db, today=MON)
    with pytest.raises(habits.HabitError) as e:
        habits.undo_last(hid, db_path=db, today=MON)
    assert e.value.code == 1


# ---- definitions, resolve, list ----

def test_validation(db):
    with pytest.raises(habits.HabitError):
        habits.create_habit(" ", "daily", db_path=db)
    with pytest.raises(habits.HabitError):
        habits.create_habit("x", "monthly", db_path=db)
    with pytest.raises(habits.HabitError):
        habits.create_habit("x", "daily", 0, db_path=db)


def test_update_pause_goal_and_owner(db):
    hid = daily(db, owner="Justin")
    h = habits.update_habit(hid, target_count=None, owner=None, active=False, db_path=db)
    assert h["target_count"] is None and h["owner"] is None and h["active"] == 0
    assert habits.list_status(db_path=db) == []
    assert len(habits.list_status(include_inactive=True, db_path=db)) == 1
    with pytest.raises(habits.HabitError):
        habits.update_habit(hid, db_path=db)


def test_resolve_by_id_name_fragment_and_ambiguity(db):
    a = daily(db, "Morning run")
    daily(db, "Evening run")
    assert habits.resolve_habit("morning run", db_path=db)["id"] == a
    assert habits.resolve_habit(a, db_path=db)["id"] == a
    assert habits.resolve_habit("morn", db_path=db)["id"] == a
    with pytest.raises(habits.HabitError) as amb:
        habits.resolve_habit("run", db_path=db)
    assert "more than one" in amb.value.message
    with pytest.raises(habits.HabitError) as nf:
        habits.resolve_habit("zzz", db_path=db)
    assert nf.value.code == 1


def test_owner_filter_includes_shared(db):
    daily(db, "J", owner="Justin")
    daily(db, "C", owner="Caroline")
    daily(db, "Shared")
    names = {s["name"] for s in habits.list_status(owner="justin", db_path=db)}
    assert names == {"J", "Shared"}


# ---- at-risk / nudge ----

def test_nudge_respects_remind_flag_and_global_switch(db):
    a = daily(db, "A")
    b = daily(db, "B", remind=False)
    for hid in (a, b):
        log(db, hid, MON - timedelta(days=1))
    assert {s["name"] for s in habits.get_at_risk(db_path=db, today=MON)} == {"A", "B"}
    assert {s["name"] for s in habits.get_at_risk(for_nudge=True, db_path=db, today=MON)} == {"A"}
    habits.set_config("nudge_enabled", "0", db_path=db)
    assert habits.get_at_risk(for_nudge=True, db_path=db, today=MON) == []
    with pytest.raises(habits.HabitError):
        habits.set_config("bogus", "1", db_path=db)
    with pytest.raises(habits.HabitError):
        habits.set_config("nudge_enabled", "maybe", db_path=db)


# ---- CLI ----

def run(capsys, *argv):
    rc = habits_store.main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_cli_round_trip(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(habits, "DB_PATH", tmp_path / "cli.db")
    rc, out, _ = run(capsys, "habit", "add", "--name", "Stretch", "--frequency", "daily", "--target", "1")
    assert rc == 0 and json.loads(out)["current_streak"] == 0
    rc, out, _ = run(capsys, "log", "add", "--habit", "stretch")
    assert rc == 0 and json.loads(out)["status"]["current_streak"] == 1
    rc, out, _ = run(capsys, "habit", "list")
    assert rc == 0 and json.loads(out)[0]["name"] == "Stretch"
    rc, out, _ = run(capsys, "log", "undo", "--habit", "stretch")
    assert rc == 0 and json.loads(out)["status"]["total_count"] == 0
    rc, out, _ = run(capsys, "habit", "update", "--habit", "stretch", "--no-goal", "--shared")
    assert rc == 0 and json.loads(out)["has_goal"] is False
    rc, out, _ = run(capsys, "at-risk", "--nudge")
    assert rc == 0 and json.loads(out) == []


def test_cli_error_codes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(habits, "DB_PATH", tmp_path / "cli.db")
    rc, _, err = run(capsys, "log", "add", "--habit", "nope")
    assert rc == 1 and "no habit" in json.loads(err)["error"]
    run(capsys, "habit", "add", "--name", "X", "--frequency", "daily")
    rc, _, err = run(capsys, "log", "add", "--habit", "X", "--date", "2999-01-01")
    assert rc == 2 and "future" in json.loads(err)["error"]


# ---- calendar / analysis ----

def test_calendar_marks_logged_days_for_one_habit(db):
    a, b = daily(db, "A"), daily(db, "B")
    log(db, a, MON)
    log(db, a, MON - timedelta(days=70))
    log(db, b, MON)
    cal = habits.calendar(a, months=2, db_path=db, today=MON)
    assert [m["label"] for m in cal["months"]] == ["September 2026", "October 2026"]
    days = {c["date"]: c for m in cal["months"] for w in m["weeks"] for c in w if c}
    assert days["2026-10-12"]["count"] == 1 and days["2026-10-13"]["count"] == 0
    assert days["2026-10-12"]["today"] and days["2026-10-13"]["future"]
    assert cal["days_done"] == 1  # the 70-days-ago log is outside the two months in view
    assert all(len(w) == 7 for m in cal["months"] for w in m["weeks"])


def test_calendar_all_habits_counts_distinct_habits_per_day(db):
    a, b = daily(db, "A"), daily(db, "B")
    log(db, a, MON); log(db, b, MON)
    cal = habits.calendar(None, months=1, db_path=db, today=MON)
    day = next(c for w in cal["months"][0]["weeks"] for c in w if c and c["date"] == "2026-10-12")
    assert day["count"] == 2 and cal["max_count"] == 2


def test_calendar_unknown_habit_raises(db):
    with pytest.raises(habits.HabitError) as e:
        habits.calendar("nope", db_path=db)
    assert e.value.code == 1


def test_analysis_rolls_up_per_habit(db):
    a = daily(db, "A")
    log(db, a, MON); log(db, a, MON - timedelta(days=1))
    out = habits.analysis(weeks=4, db_path=db, today=MON)
    row = out["habits"][0]
    assert row["days_done"] == 2 and row["last_30_days"] == 2 and len(row["weekly_days"]) == 4
    assert row["weekly_days"][-1] == 1 and row["weekly_days"][-2] == 1
    assert row["best_weekday"] in ("Sun", "Mon") and out["active_days"] == 2


def test_cli_calendar_and_analysis(db, capsys, monkeypatch):
    monkeypatch.setattr(habits, "DB_PATH", db)
    hid = daily(db, "A")
    log(db, hid, date.today())
    assert habits_store.main(["calendar", "--habit", "A"]) == 0
    assert json.loads(capsys.readouterr().out)["days_done"] == 1
    assert habits_store.main(["analysis"]) == 0
    assert json.loads(capsys.readouterr().out)["habits"][0]["name"] == "A"
