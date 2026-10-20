import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import dayrating  # noqa: E402
import habits  # noqa: E402
import insights  # noqa: E402
import journal  # noqa: E402
import journal_store  # noqa: E402
from insight_sources import journal as journal_source  # noqa: E402

TODAY = date(2026, 10, 14)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "t.db"


def back(n):
    return TODAY - timedelta(days=n)


def test_defaults_seeded_once_and_resolve(db):
    qs = journal.list_questions(db_path=db)
    assert [q["slug"] for q in qs][:3] == ["alcohol", "caffeine_late", "thc"] and len(qs) == 9
    assert len(journal.list_questions(db_path=db)) == 9
    assert journal.resolve_question("caffeine", db_path=db)["slug"] == "caffeine_late"
    assert journal.resolve_question("Screens in bed", db_path=db)["slug"] == "screens_bed"
    with pytest.raises(journal.JournalError) as e:
        journal.resolve_question("zzz", db_path=db)
    assert e.value.code == 1


def test_add_pause_question(db):
    q = journal.add_question("Nap", category="sleep", db_path=db)
    assert q["slug"] == "nap" and q["text"] == "Nap?"
    with pytest.raises(journal.JournalError):
        journal.add_question("nap?", db_path=db)
    journal.update_question("nap", active=False, db_path=db)
    assert "nap" not in [x["slug"] for x in journal.list_questions(db_path=db)]
    assert journal.resolve_question("nap", db_path=db)["active"] == 0


def test_answers_upsert_clear_and_validation(db):
    journal.set_answer("alcohol", "yes", answer_date=back(1), db_path=db, today=TODAY)
    journal.set_answer("alcohol", "no", answer_date=back(1), db_path=db, today=TODAY)
    assert journal.answers_for(back(1), db_path=db, today=TODAY) == {"alcohol": 0}
    with pytest.raises(journal.JournalError):
        journal.set_answer("alcohol", "maybe", db_path=db, today=TODAY)
    with pytest.raises(journal.JournalError):
        journal.set_answer("alcohol", "yes", answer_date=TODAY + timedelta(days=1), db_path=db, today=TODAY)
    journal.clear_answer("alcohol", answer_date=back(1), db_path=db, today=TODAY)
    assert journal.answers_for(back(1), db_path=db, today=TODAY) == {}
    with pytest.raises(journal.JournalError) as e:
        journal.clear_answer("alcohol", answer_date=back(1), db_path=db, today=TODAY)
    assert e.value.code == 1


def test_unanswered_days_stay_out_of_source_and_history(db):
    journal.set_answer("alcohol", "yes", answer_date=back(2), db_path=db, today=TODAY)
    journal.set_answer("alcohol", "no", answer_date=back(1), db_path=db, today=TODAY)
    conn = journal.connect(db)
    out = journal_source.collect(conn, back(5), TODAY, TODAY)
    conn.close()
    assert out == {"journal:alcohol": {back(2).isoformat(): 1.0, back(1).isoformat(): 0.0}}
    h = journal.history(days=3, db_path=db, today=TODAY)
    row = next(r for r in h["questions"] if r["slug"] == "alcohol")
    assert [c["answer"] for c in row["cells"]] == ["yes", "no", None] and h["answered_days"] == 2


def test_import_from_habit(db):
    hid = habits.create_habit("Alcohol", "daily", None, db_path=db)["id"]
    for d in (back(3), back(1)):
        habits.log_completion(hid, log_date=d.isoformat(), db_path=db, today=TODAY)
    journal.set_answer("alcohol", "no", answer_date=back(1), db_path=db, today=TODAY)  # existing answer wins
    r = journal.import_from_habit("Alcohol", "alcohol", db_path=db)
    assert r["days_logged"] == 2 and r["answers_added"] == 1 and r["habit_paused"]
    assert journal.answers_for(back(3), db_path=db, today=TODAY) == {"alcohol": 1}
    assert journal.answers_for(back(2), db_path=db, today=TODAY) == {}      # unlogged day stays unanswered
    assert habits.get_status(hid, db_path=db, today=TODAY)["active"] is False


def test_checkin_pending_message_and_silence(db):
    hid = habits.create_habit("Stretch", "daily", 1, db_path=db)["id"]
    now = datetime(2026, 10, 14, 20, 30)
    msg = journal.checkin_message(now=now, db_path=db)["message"]
    assert msg.startswith("🌙 Check-in for Wednesday, Oct 14") and "Rate today" in msg
    assert "Alcohol · Caffeine after 2 PM" in msg and "Not logged yet: Stretch" in msg
    assert "Yesterday isn't rated" in msg
    journal.record(score=7, note="ok", rest="no", habits_done=["stretch"], db_path=db, today=TODAY)
    assert journal.checkin_message(now=now, db_path=db)["message"] is None
    assert habits.get_status(hid, db_path=db, today=TODAY)["done_this_period"] == 1


def test_record_reply(db):
    hid = habits.create_habit("Stretch", "daily", 1, db_path=db)["id"]
    habits.log_completion(hid, db_path=db, today=TODAY)
    r = journal.record(score="8", note="good", yes=["alcohol", "late meal"], no=["thc"], habits_done=["Stretch"],
                       db_path=db, today=TODAY)
    assert r["recorded"]["rating"] == 8 and r["recorded"]["habits_logged"] == []   # already logged: not doubled
    assert r["recorded"]["answers"] == {"alcohol": "yes", "late_meal": "yes", "thc": "no"}
    assert len(r["pending"]["unanswered"]) == 6 and not r["pending"]["complete"]
    r = journal.record(rest="no", db_path=db, today=TODAY)
    assert r["pending"]["unanswered"] == [] and r["pending"]["complete"]
    assert journal.answers_for(db_path=db, today=TODAY)["alcohol"] == 1             # rest didn't overwrite
    r = journal.record(note="actually great", db_path=db, today=TODAY)
    assert dayrating.get_rating(db_path=db, today=TODAY)["note"] == "actually great"
    assert dayrating.get_rating(db_path=db, today=TODAY)["score"] == 8


def test_record_validates_before_writing(db):
    # each bad reply also carries a valid answer, which must not be written either
    for bad in ({"score": 11}, {"no": ["alcohol"]}, {"no": ["nope"]}, {"habits_done": ["nope"]},
                {"note": "no rating yet"}, {"rest": "maybe"}):
        with pytest.raises(journal.JournalError):
            journal.record(yes=["alcohol"], db_path=db, today=TODAY, **bad)
    assert journal.answers_for(db_path=db, today=TODAY) == {}
    assert dayrating.get_rating(db_path=db, today=TODAY) is None


def test_checkin_date_timing(db):
    morning = datetime(2026, 10, 14, 9, 0)
    assert journal.checkin_date(morning, db_path=db) == back(1)            # yesterday unfinished: catch up
    journal.record(day=back(1), score=6, rest="no", db_path=db, today=TODAY)
    assert journal.checkin_date(morning, db_path=db) == TODAY
    assert journal.checkin_date(datetime(2026, 10, 14, 18, 0), db_path=db) == TODAY


def test_impact_needs_five_each_and_uses_next_night(db):
    hrv = {}
    for i in range(1, 13):
        d = back(i + 1)
        yes = i % 2 == 0
        journal.set_answer("alcohol", yes, answer_date=d, db_path=db, today=TODAY)
        dayrating.set_rating(6, rating_date=d.isoformat(), db_path=db, today=TODAY)
        hrv[(d + timedelta(days=1)).isoformat()] = 38.0 if yes else 46.0  # filed on the wake date: d + 1
    conn = journal.connect(db)
    conn.executemany("INSERT INTO insight_daily VALUES ('coros:hrv_ms', ?, ?, 'coros_wellness', 'x')", hrv.items())
    conn.commit()
    conn.close()
    imp = journal.impact(db_path=db, today=TODAY)
    q = next(x for x in imp["questions"] if x["slug"] == "alcohol")
    hrv = next(t for t in q["targets"] if t["target"] == "hrv")
    assert hrv["enough"] and hrv["avg_with"] == 38.0 and hrv["avg_without"] == 46.0 and hrv["effect"] == -8.0
    assert hrv["verdict"] == "bad" and hrv["confidence"] in ("solid", "emerging", "hint")
    rating = next(t for t in q["targets"] if t["target"] == "rating")
    assert rating["enough"] and rating["effect"] == 0 and rating["verdict"] == "neutral"
    sleep = next(t for t in q["targets"] if t["target"] == "sleep_score")
    assert not sleep["enough"] and sleep["yes_n"] == 0
    thc = next(x for x in imp["questions"] if x["slug"] == "thc")
    assert thc["yes_days"] == 0 and not any(t["enough"] for t in thc["targets"])


def test_journal_metrics_reach_rating_analysis(db):
    for i in range(12):
        d = back(i)
        dayrating.set_rating(8 if i % 2 else 4, rating_date=d.isoformat(), db_path=db, today=TODAY)
        journal.set_answer("stressful", "no" if i % 2 else "yes", answer_date=d, db_path=db, today=TODAY)
    insights.refresh(days=30, only="journal", db_path=db, today=TODAY)
    f = next(f for f in insights.analyze(db_path=db, today=TODAY)["findings"] if f["metric"] == "journal:stressful")
    assert f["kind"] == "yes_no" and f["effect"] == -4.0


def run_cli(monkeypatch, capsys, db, *argv):
    monkeypatch.setattr(journal.dayrating, "DB_PATH", db)
    code = journal_store.main(list(argv))
    out = capsys.readouterr()
    return code, (json.loads(out.out) if out.out.strip() else None), out.err


def test_cli(monkeypatch, capsys, db):
    code, out, _ = run_cli(monkeypatch, capsys, db, "checkin", "message")
    assert code == 0 and "Check-in" in out["message"]
    code, out, _ = run_cli(monkeypatch, capsys, db, "checkin", "record", "--score", "7", "--yes", "alcohol",
                           "--rest", "no")
    assert code == 0 and out["pending"]["complete"]
    code, out, _ = run_cli(monkeypatch, capsys, db, "checkin", "message")
    assert out == {"message": None}
    code, out, _ = run_cli(monkeypatch, capsys, db, "answer", "--question", "thc", "--value", "yes")
    assert code == 0 and out["answer"] == "yes"
    code, out, _ = run_cli(monkeypatch, capsys, db, "question", "add", "--text", "Nap?")
    assert code == 0 and out["slug"] == "nap"
    code, out, _ = run_cli(monkeypatch, capsys, db, "history", "--days", "7")
    assert code == 0 and len(out["dates"]) == 7
    code, out, _ = run_cli(monkeypatch, capsys, db, "impact")
    assert code == 0 and out["min_each"] == 5
    code, _, err = run_cli(monkeypatch, capsys, db, "answer", "--question", "zzz", "--value", "yes")
    assert code == 1 and "no question" in json.loads(err)["error"]
    code, _, err = run_cli(monkeypatch, capsys, db, "checkin", "record", "--score", "12")
    assert code == 2
