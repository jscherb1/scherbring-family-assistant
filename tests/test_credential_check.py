import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import credential_check as cc  # noqa: E402

NOW = datetime(2026, 10, 20, 9, 30).astimezone()
HV = cc.System("test", "Test", 11, "run login_test.py --login-only", "Test Login")  # generic 11-day login


def login(days_ago):
    return NOW - timedelta(days=days_ago)


def test_quiet_when_fresh():
    msg, entry = cc.evaluate(HV, {}, "ok", NOW, login(3))
    assert msg is None and entry["lifetime_days"] == 11


def test_past_timer_but_probe_ok_sends_nothing():
    for days in (9.5, 12, 40):
        assert cc.evaluate(HV, {}, "ok", NOW, login(days))[0] is None


def test_no_advance_expiry_warning():
    msg, _ = cc.evaluate(HV, {}, "ok", NOW, login(9.2))
    assert msg is None


def test_dead_probe_alerts_now_and_learns_a_shorter_lifetime():
    msg, entry = cc.evaluate(HV, {}, "dead", NOW, login(6))
    assert msg.startswith("\U0001f534") and "EXPIRED" in msg
    assert entry["lifetime_days"] == 6.0  # died at 6 days, not the assumed 11
    assert 'Run "Test Login"' in msg or '"Test Login"' in msg
    assert "login_test.py --login-only" in msg  # terminal fallback


def test_dead_probe_rate_limited_to_once_a_day():
    _, entry = cc.evaluate(HV, {}, "dead", NOW, login(6))
    again, _ = cc.evaluate(HV, entry, "dead", NOW + timedelta(hours=3), login(6))
    assert again is None
    later, _ = cc.evaluate(HV, entry, "dead", NOW + timedelta(hours=21), login(6))
    assert later is not None


def test_dead_past_timer_adds_quiet_note():
    msg, _ = cc.evaluate(HV, {}, "dead", NOW, login(13))
    assert "past the usual 11 days" in msg


def test_dead_after_full_lifetime_does_not_shrink_estimate():
    _, entry = cc.evaluate(HV, {}, "dead", NOW, login(13))
    assert entry["lifetime_days"] == 11


def test_unknown_probe_never_claims_expiry():
    msg, entry = cc.evaluate(HV, {}, "unknown", NOW, login(3))
    assert msg is None and entry["status"] == "unknown"


def test_recovery_rearms_alert():
    _, entry = cc.evaluate(HV, {}, "dead", NOW, login(10))
    assert "last_alert_at" in entry
    _, entry = cc.evaluate(HV, entry, "ok", NOW + timedelta(days=1), login(0.1))
    assert "last_alert_at" not in entry


def test_run_end_to_end_with_fakes(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(cc, "authed_at", lambda key, entry: login(10))
    sent = []
    ok = {k: (lambda: "ok") for k in cc.SYSTEMS}
    cc.run(probes=ok, now=NOW, send=lambda t: sent.append(t) or True)
    assert sent == []  # old logins that still work are silent
    probes = {**ok, "drive": lambda: "dead"}
    out = cc.run(probes=probes, now=NOW, send=lambda t: sent.append(t) or True)
    assert len(sent) == 1 and "Google Drive Login" in sent[0]
    assert any("-> message" in l for l in out)
    cc.run(probes=probes, now=NOW + timedelta(hours=2), send=lambda t: sent.append(t) or True)
    assert len(sent) == 1  # same day: no repeat
    before = (tmp_path / "state.json").read_text()
    cc.run(probes=probes, now=NOW + timedelta(days=3), send=lambda t: sent.append(t) or True, dry_run=True)
    assert len(sent) == 1 and (tmp_path / "state.json").read_text() == before


def test_failed_delivery_is_retried_next_run(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(cc, "authed_at", lambda key, entry: login(10))
    probes = {k: (lambda: "dead") for k in cc.SYSTEMS}
    cc.run(probes=probes, now=NOW, send=lambda t: False)
    sent = []
    cc.run(probes=probes, now=NOW + timedelta(minutes=5), send=lambda t: sent.append(t) or True)
    assert sent  # not suppressed by the failed attempt


def test_record_login_sets_authed_at_and_clears_alert(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "STATE_FILE", tmp_path / "state.json")
    (tmp_path / "state.json").write_text('{"hyvee": {"authed_at": "2020-01-01T00:00:00+00:00", "last_alert_at": "2020-01-02T00:00:00+00:00"}}')
    cc.record_login("hyvee", NOW)
    import json
    e = json.loads((tmp_path / "state.json").read_text())["hyvee"]
    assert e["authed_at"].startswith("2026-10-20") and "last_alert_at" not in e


def test_dead_probe_on_a_brand_new_login_does_not_shrink_estimate():
    _, entry = cc.evaluate(HV, {}, "dead", NOW, login(0.2))
    assert entry["lifetime_days"] == 11
