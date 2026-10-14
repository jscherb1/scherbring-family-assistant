import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import myhub  # noqa: E402
import system_health as sh  # noqa: E402
import system_health_store  # noqa: E402


@pytest.fixture
def db(tmp_path):
    return tmp_path / "t.db"


def by_key(db):
    return {s["key"]: s for s in sh.get_status(db_path=db)}


def test_every_known_system_listed_even_unchecked(db):
    s = by_key(db)
    assert set(s) == set(sh.SYSTEMS)
    assert s["gmail"]["status"] == "unknown" and "not checked" in s["gmail"]["detail"]


def test_one_failure_is_warn_two_in_a_row_is_down_and_ok_resets(db):
    assert sh.record("open_meteo", "warn", "x", db_path=db)["status"] == "warn"
    assert sh.record("open_meteo", "warn", "x", db_path=db)["status"] == "down"
    r = sh.record("open_meteo", "ok", "fine", db_path=db)
    assert r["status"] == "ok" and r["fail_streak"] == 0
    assert by_key(db)["open_meteo"]["last_ok_at"]


def test_unknown_keeps_previous_verdict(db):
    sh.record("drive", "down", "expired", db_path=db)
    assert sh.record("drive", "unknown", "probe flaked", db_path=db)["status"] == "down"


def test_bad_input_raises_typed_error(db):
    with pytest.raises(sh.HealthError):
        sh.record("nope", "ok", db_path=db)
    with pytest.raises(sh.HealthError):
        sh.record("drive", "great", db_path=db)
    with pytest.raises(sh.HealthError):
        sh.run_checks(["gmail"], db_path=db)


def test_crashing_check_is_recorded_not_raised(db, monkeypatch):
    def boom():
        raise RuntimeError("kaput")
    monkeypatch.setitem(sh.SCRIPT_CHECKS, "open_meteo", boom)
    out = sh.run_checks(["open_meteo"], db_path=db)
    assert out[0]["status"] == "warn" and "kaput" in out[0]["detail"]


def test_todoist_inferred_from_hub_snapshot(db):
    import myhub as m
    m.set_snapshot("task", [{"title": "a", "day": "2026-10-14"}], db_path=db)
    s = by_key(db)["todoist"]
    assert s["status"] == "ok" and "inferred" in s["detail"]


def test_down_system_reaches_hub_attention_and_tile(db):
    sh.record("coros_mcp", "down", "needs sign-in", db_path=db)
    h = myhub.get_hub(db_path=db, today=date(2026, 10, 14))
    assert h["errors"] == {}
    assert any("COROS (health) is down" in a["text"] for a in h["attention"])
    tile = {s["key"]: s for s in h["cards"]["system"]["logins"]}
    assert set(tile) == set(sh.SYSTEMS)  # hub and service see the same systems


def test_cli_status_and_record(db, monkeypatch, capsys):
    monkeypatch.setattr(sh, "connect", lambda p=None: sh.dayrating.connect(db))
    assert system_health_store.main(["record", "gmail", "ok", "--detail", "read ok"]) == 0
    capsys.readouterr()
    assert system_health_store.main(["status"]) == 0
    assert {s["key"]: s for s in json.loads(capsys.readouterr().out)}["gmail"]["status"] == "ok"
    assert system_health_store.main(["record", "bogus", "ok"]) == 2
