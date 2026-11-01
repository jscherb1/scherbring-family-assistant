import json
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import review  # noqa: E402
import review_store  # noqa: E402
import scheduler_dispatch as sd  # noqa: E402
import scheduler_store  # noqa: E402

TODAY = date(2026, 11, 7)  # a Saturday


@pytest.fixture
def db(tmp_path):
    return tmp_path / "t.db"


def finding(key="sleep-vs-calendar", **kw):
    base = {"key": key, "domain": "cross-domain", "title": "Late events cut into sleep",
            "why_it_matters": "Short sleep follows.", "evidence": "Three late events this week.",
            "suggested_action": "Move one."}
    return {**base, **kw}


# ---- save_run and findings ----

def test_save_run_creates_findings_as_new(db):
    out = review.save_run("Two things.", [finding(), finding("passport", domain="family", title="Passport")], db_path=db)
    assert out["saved"] and [f["result"] for f in out["findings"]] == ["created", "created"]
    rows = review.list_findings(db_path=db)
    assert {f["key"] for f in rows} == {"sleep-vs-calendar", "passport"}
    assert all(f["status"] == "new" and f["times_raised"] == 1 for f in rows)
    assert all(len(f["id"]) == 36 and f["created_at"][4] == "-" and "T" in f["created_at"] for f in rows)


def test_empty_run_is_valid(db):
    out = review.save_run("Nothing new this week.", [], db_path=db)
    assert out["findings"] == []
    latest = review.latest_run(db_path=db)
    assert latest["summary"] == "Nothing new this week." and latest["findings"] == [] and latest["finding_count"] == 0


def test_at_most_five_findings(db):
    five = [finding(f"key-{i}") for i in range(5)]
    assert len(review.save_run("Five.", five, db_path=db)["findings"]) == 5
    with pytest.raises(review.ReviewError) as exc:
        review.save_run("Six.", [finding(f"other-{i}") for i in range(6)], db_path=db)
    assert exc.value.code == 2 and "at most 5" in exc.value.message
    assert len(review.list_findings(db_path=db)) == 5  # the rejected run saved nothing


@pytest.mark.parametrize("bad", [
    {"key": ""}, {"key": "Has Spaces"}, {"key": "ab"}, {"domain": "weather"}, {"domain": ""},
    {"title": ""}, {"why_it_matters": " "}, {"evidence": ""}, {"suggested_action": ""},
    {"title": "x" * 141}, {"evidence": "x" * 901},
])
def test_finding_validation(db, bad):
    with pytest.raises(review.ReviewError) as exc:
        review.save_run("S.", [finding(**bad)], db_path=db)
    assert exc.value.code == 2
    assert review.list_findings(db_path=db) == []


def test_run_level_validation(db):
    for summary, findings in (("", []), ("  ", []), ("S", "nope"), ("S", ["nope"]),
                              ("S", [finding("same-key"), finding("same-key")]), ("x" * 1201, [])):
        with pytest.raises(review.ReviewError) as exc:
            review.save_run(summary, findings, db_path=db)
        assert exc.value.code == 2


def test_same_key_updates_instead_of_duplicating(db):
    first = review.save_run("One.", [finding(evidence="Three late events.")], db_path=db)
    fid = first["findings"][0]["id"]
    second = review.save_run("Again.", [finding(evidence="Five late events now.")], db_path=db)
    assert second["findings"][0] == {"id": fid, "key": "sleep-vs-calendar", "result": "updated", "status": "new"}
    rows = review.list_findings(db_path=db)
    assert len(rows) == 1 and rows[0]["evidence"] == "Five late events now." and rows[0]["times_raised"] == 2
    assert rows[0]["run_id"] == second["run_id"]
    assert [f["id"] for f in review.latest_run(db_path=db)["findings"]] == [fid]


def test_reraising_accepted_keeps_status_but_dismissed_and_done_reopen(db):
    fid = review.save_run("One.", [finding()], db_path=db)["findings"][0]["id"]
    review.set_status(fid, "accepted", db_path=db)
    assert review.save_run("Two.", [finding()], db_path=db)["findings"][0]["status"] == "accepted"
    for status in ("dismissed", "done"):
        review.set_status(fid, status, db_path=db)
        out = review.save_run("Again.", [finding()], db_path=db)["findings"][0]
        assert out["result"] == "reopened" and out["status"] == "new"


def test_set_status_by_id_or_key_and_errors(db):
    fid = review.save_run("One.", [finding()], db_path=db)["findings"][0]["id"]
    assert review.set_status(fid, "dismissed", db_path=db)["status"] == "dismissed"
    assert review.set_status("sleep-vs-calendar", "done", db_path=db)["status"] == "done"
    with pytest.raises(review.ReviewError) as bad:
        review.set_status(fid, "ignored", db_path=db)
    assert bad.value.code == 2
    with pytest.raises(review.ReviewError) as missing:
        review.set_status("nope", "done", db_path=db)
    assert missing.value.code == 1


def test_list_findings_filters_and_counts(db):
    ids = {f["key"]: f["id"] for f in review.save_run(
        "Three.", [finding("a-one"), finding("b-two"), finding("c-three")], db_path=db)["findings"]}
    review.set_status(ids["a-one"], "accepted", db_path=db)
    review.set_status(ids["b-two"], "dismissed", db_path=db)
    assert [f["key"] for f in review.list_findings(status="new", db_path=db)] == ["c-three"]
    assert review.counts(db_path=db)["accepted"] == 1 and review.counts(db_path=db)["dismissed"] == 1
    assert review.counts(db_path=db)["last_run_at"]
    with pytest.raises(review.ReviewError):
        review.list_findings(status="bogus", db_path=db)


def test_latest_run_with_no_runs_is_not_found(db):
    with pytest.raises(review.ReviewError) as exc:
        review.latest_run(db_path=db)
    assert exc.value.code == 1


# ---- gather ----

def seed(db):
    conn = review.connect(db)
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("INSERT INTO hub_items (id,kind,day,start_time,end_time,title,source,synced_at) VALUES "
                 "('e1','event',?,'09:00','10:00','Dentist','Family',?)", (TODAY.isoformat(), now))
    conn.execute("INSERT INTO hub_items (id,kind,day,start_time,end_time,title,source,synced_at) VALUES "
                 "('e2','event',?,'09:30','10:30','Soccer','Kids',?)", (TODAY.isoformat(), now))
    conn.execute("INSERT INTO hub_items (id,kind,day,title,source,priority,synced_at) VALUES "
                 "('t1','task',?,'Renew passport','Inbox',1,?)", ((TODAY - timedelta(days=9)).isoformat(), now))
    conn.execute("INSERT INTO kid_memories (id,created_at,memory_date,children_json,raw_text,memory_text,source) "
                 "VALUES ('k1',?,?,?,?,?,'direct')",
                 (now, TODAY.isoformat(), '["Ruth"]', "KIDSECRET-raw", "KIDSECRET-text"))
    conn.execute("INSERT INTO agent_results (agent,task,created_at,summary) VALUES ('kids-memory','t',?,'KIDSECRET-agent')",
                 (TODAY.isoformat() + "T08:00:00",))
    conn.execute("INSERT INTO agent_results (agent,task,created_at,summary) VALUES ('todoist','t',?,'added milk')",
                 (TODAY.isoformat() + "T08:00:00",))
    conn.execute("INSERT INTO profile_people (id,name,relationship,birthday,notes,created_at,updated_at) VALUES "
                 "('p1','Grandma Jo','relative','11-20','PROFILESECRET-notes',?,?)", (now, now))
    conn.execute("INSERT INTO profile_people_facts (id,person_id,key,value,updated_at) VALUES "
                 "('f1','p1','allergy','PROFILESECRET-allergy',?)", (now,))
    conn.execute("INSERT INTO profile_facts (key,value,updated_at) VALUES ('home_address','PROFILESECRET-addr',?)", (now,))
    conn.execute("INSERT INTO profile_facts (key,value,updated_at) VALUES ('wedding_anniversary','11-15',?)", (now,))
    conn.commit()
    conn.close()


def test_gather_covers_every_domain_and_is_json_bounded(db):
    seed(db)
    review.save_run("Last week.", [finding(title="Old finding")], db_path=db)
    d = review.gather(db_path=db, today=TODAY)
    assert set(d["sections"]) == set(review.SECTIONS)
    assert d["failed_sections"] == [] and d["weekday"] == "Saturday"
    cal = d["sections"]["calendar"]
    assert cal["events_next_14d"] == 2 and cal["overlapping_timed_events"][0]["day"] == TODAY.isoformat()
    tasks = d["sections"]["tasks"]
    assert tasks["overdue_count"] == 1 and tasks["oldest_overdue_days"] == 9
    assert d["digest_chars"] <= review.MAX_CHARS
    json.dumps(d)  # serialisable
    assert d["previous_findings"]["findings"][0]["title"] == "Old finding"
    assert d["previous_findings"]["findings"][0]["status"] == "new"


def test_gather_includes_dismissals_so_runs_dont_repeat(db):
    fid = review.save_run("One.", [finding()], db_path=db)["findings"][0]["id"]
    review.set_status(fid, "dismissed", db_path=db)
    prev = review.gather(db_path=db, today=TODAY)["previous_findings"]
    assert prev["findings"][0]["key"] == "sleep-vs-calendar" and prev["findings"][0]["status"] == "dismissed"
    assert prev["open_counts"]["dismissed"] == 1


def test_gather_leaks_no_kid_memories_or_profile_values(db):
    seed(db)
    text = json.dumps(review.gather(db_path=db, today=TODAY))
    assert "KIDSECRET" not in text and "PROFILESECRET" not in text
    dates = review.gather(db_path=db, today=TODAY)["sections"]["profile_dates"]
    assert {u["what"] for u in dates["upcoming_45_days"]} == {"Grandma Jo birthday", "wedding anniversary"}
    assert dates["upcoming_45_days"][0]["days"] == 8
    assert "todoist" in review.gather(db_path=db, today=TODAY)["sections"]["agent_results"]["results_7d_by_agent"]


def test_a_failing_section_becomes_an_error_not_a_crash(db, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("money exploded")
    monkeypatch.setitem(review.SECTIONS, "money", boom)
    d = review.gather(db_path=db, today=TODAY)
    assert d["sections"]["money"] == {"error": "RuntimeError: money exploded"}
    assert d["failed_sections"] == ["money"]
    assert "error" not in d["sections"]["calendar"]


def test_gather_shrinks_to_the_bound(db):
    conn = review.connect(db)
    now = datetime.now().isoformat(timespec="seconds")
    for i in range(300):
        conn.execute("INSERT INTO hub_items (id,kind,day,start_time,title,source,synced_at) VALUES (?,?,?,?,?,?,?)",
                     (f"e{i}", "event", TODAY.isoformat(), f"{i % 24:02d}:00", "Long title " * 30, "Cal", now))
        conn.execute("INSERT INTO agent_results (agent,task,created_at,summary) VALUES (?,?,?,?)",
                     (f"agent{i}", "t", TODAY.isoformat() + "T08:00:00", "summary " * 200))
    conn.commit()
    conn.close()
    d = review.gather(db_path=db, today=TODAY, max_chars=6000)
    assert d["digest_chars"] <= 6000 and "more" in json.dumps(d["sections"]["calendar"]["events"][-1])


# ---- CLI ----

def run_cli(capsys, *argv):
    rc = review_store.main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_cli_round_trip(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(review, "DB_PATH", tmp_path / "cli.db")
    payload = tmp_path / "run.json"
    payload.write_text(json.dumps({"summary": "One thing.", "findings": [finding()]}))
    rc, out, _ = run_cli(capsys, "save", "--file", str(payload))
    assert rc == 0 and json.loads(out)["findings"][0]["result"] == "created"
    rc, out, _ = run_cli(capsys, "findings", "--status", "new")
    fid = json.loads(out)[0]["id"]
    rc, out, _ = run_cli(capsys, "status", "--id", fid, "--status", "accepted")
    assert rc == 0 and json.loads(out)["status"] == "accepted"
    rc, out, _ = run_cli(capsys, "latest")
    assert json.loads(out)["findings"][0]["status"] == "accepted"
    rc, out, _ = run_cli(capsys, "gather")
    digest = json.loads(out)
    assert rc == 0 and digest["digest_chars"] <= review.MAX_CHARS and "\n" not in out.strip()
    assert digest["previous_findings"]["findings"][0]["status"] == "accepted"


def test_cli_error_codes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(review, "DB_PATH", tmp_path / "cli.db")
    rc, _, err = run_cli(capsys, "latest")
    assert rc == 1 and "no review has run yet" in json.loads(err)["error"]
    rc, _, err = run_cli(capsys, "status", "--id", "nope", "--status", "done")
    assert rc == 1
    rc, _, err = run_cli(capsys, "status", "--id", "nope", "--status", "bogus")
    assert rc == 2
    rc, _, err = run_cli(capsys, "save", "--file", str(tmp_path / "missing.json"))
    assert rc == 2 and "cannot read" in json.loads(err)["error"]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"summary": "S", "findings": [finding(f"k-{i}") for i in range(6)]}))
    rc, _, err = run_cli(capsys, "save", "--file", str(bad))
    assert rc == 2 and "at most 5" in json.loads(err)["error"]
    bad.write_text("[1, 2]")
    assert run_cli(capsys, "save", "--file", str(bad))[0] == 2


# ---- per-task timeout_seconds (scheduler) ----

@pytest.fixture
def sched(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler_store, "DB_PATH", tmp_path / "sched.db")
    return tmp_path / "sched.db"


def store(capsys, *argv):
    rc = scheduler_store.main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


ADD = ("add", "--name", "weekly-review", "--prompt", "p", "--cron", "0 9 * * 6", "--target-chat-id", "1")


def test_timeout_defaults_to_null_and_can_be_set_on_add(sched, capsys):
    rc, out, _ = store(capsys, *ADD)
    assert rc == 0 and json.loads(out)["timeout_seconds"] is None
    rc, out, _ = store(capsys, "add", "--name", "long", "--prompt", "p", "--cron", "0 9 * * 6",
                       "--target-chat-id", "1", "--timeout-seconds", "1200")
    assert rc == 0 and json.loads(out)["timeout_seconds"] == 1200
    assert json.loads(store(capsys, "get", "--name", "long")[1])["timeout_seconds"] == 1200


def test_timeout_update_set_clear_and_validate(sched, capsys):
    store(capsys, *ADD)
    rc, out, _ = store(capsys, "update", "--name", "weekly-review", "--timeout-seconds", "1200")
    assert rc == 0 and json.loads(out)["timeout_seconds"] == 1200
    rc, out, _ = store(capsys, "update", "--name", "weekly-review", "--timeout-seconds", "0")
    assert rc == 0 and json.loads(out)["timeout_seconds"] is None
    for bad in ("5", "99999", "-1"):
        rc, _, err = store(capsys, "update", "--name", "weekly-review", "--timeout-seconds", bad)
        assert rc == 2 and "between 30 and 7200" in json.loads(err)["error"]
    rc, _, err = store(capsys, "add", "--name", "x", "--prompt", "p", "--cron", "* * * * *",
                       "--target-chat-id", "1", "--timeout-seconds", "10")
    assert rc == 2


def test_due_carries_timeout_to_the_dispatcher(sched, capsys):
    store(capsys, "add", "--name", "long", "--prompt", "p", "--cron", "0 9 * * *", "--target-chat-id", "1",
          "--timeout-seconds", "1200")
    rc, out, _ = store(capsys, "due", "--now", (datetime.now() + timedelta(days=2)).isoformat(timespec="seconds"))
    assert rc == 0 and json.loads(out)[0]["timeout_seconds"] == 1200


def test_migration_adds_the_column_to_an_existing_database(sched, capsys):
    """A database created before timeout_seconds existed keeps its rows and gains the column on connect."""
    old = sqlite3.connect(sched)
    old.executescript("""
        CREATE TABLE scheduled_tasks (
            id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, prompt TEXT NOT NULL, cron_expression TEXT NOT NULL,
            target_chat_id TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
            last_dispatched_at TEXT);
        INSERT INTO scheduled_tasks VALUES ('t1','old-task','p','0 8 * * *','1',1,'2026-01-01T00:00:00',NULL);
    """)
    old.commit()
    old.close()
    rc, out, _ = store(capsys, "get", "--name", "old-task")
    assert rc == 0 and json.loads(out)["timeout_seconds"] is None and json.loads(out)["prompt"] == "p"
    # connecting again is a no-op (idempotent), and the new column is usable
    assert store(capsys, "update", "--name", "old-task", "--timeout-seconds", "600")[0] == 0
    assert json.loads(store(capsys, "list")[1])[0]["timeout_seconds"] == 600


def test_dispatcher_uses_the_task_timeout_or_the_default(monkeypatch, tmp_path):
    monkeypatch.setattr(sd, "DISPATCH_DEBUG_DIR", tmp_path)
    monkeypatch.setattr(sd, "checkout_problem", lambda *a, **k: "")
    seen = []

    def fake_run(args, **kw):
        seen.append(kw["timeout"])
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(sd.subprocess, "run", fake_run)
    base = {"id": "t1", "name": "n", "prompt": "p", "due_run_at": "r"}
    for extra in ({}, {"timeout_seconds": None}, {"timeout_seconds": 0}, {"timeout_seconds": "junk"},
                  {"timeout_seconds": 1200}):
        sd.dispatch_task({**base, **extra})
    assert seen == [sd.TASK_TIMEOUT_SECONDS] * 4 + [1200]


def test_timed_out_run_reports_its_own_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(sd, "DISPATCH_DEBUG_DIR", tmp_path)

    def fake_run(args, **kw):
        raise subprocess.TimeoutExpired(args, kw["timeout"])

    monkeypatch.setattr(sd.subprocess, "run", fake_run)
    status, summary = sd.dispatch_task({"id": "t1", "name": "n", "prompt": "p", "due_run_at": "r",
                                        "timeout_seconds": 1200})
    assert status == "failed" and summary == "timed out after 1200s"


def test_heartbeat_keeps_beating_during_a_long_task(monkeypatch):
    beats = []
    monkeypatch.setattr(sd, "update_heartbeat", lambda: beats.append(1))
    monkeypatch.setattr(sd, "HEARTBEAT_EVERY_SECONDS", 0.01)
    import time
    with sd.keep_heartbeat():
        time.sleep(0.15)
    count = len(beats)
    assert count >= 2
    time.sleep(0.05)
    assert len(beats) == count  # stopped on exit
