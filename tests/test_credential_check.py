import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import credential_check as cc  # noqa: E402

NOW = datetime(2026, 10, 20, 9, 30).astimezone()
HV = cc.SYSTEMS["hyvee"]  # lifetime 11 days


def login(days_ago):
    return NOW - timedelta(days=days_ago)


def test_quiet_when_fresh():
    msg, entry = cc.evaluate(HV, {}, "ok", NOW, login(3))
    assert msg is None and entry["lifetime_days"] == 11


def test_reminds_two_days_ahead_with_command():
    msg, entry = cc.evaluate(HV, {}, "ok", NOW, login(9.2))
    assert msg and "expected to expire in about 2 day" in msg
    assert "login_test.py --login-only" in msg
    assert entry["last_alert_at"]


def test_reminder_is_rate_limited_to_once_a_day_then_repeats():
    _, entry = cc.evaluate(HV, {}, "ok", NOW, login(10))
    again, _ = cc.evaluate(HV, entry, "ok", NOW + timedelta(hours=3), login(10))
    assert again is None
    later, _ = cc.evaluate(HV, entry, "ok", NOW + timedelta(hours=21), login(10))
    assert later is not None


def test_past_lifetime_but_still_working_says_may_stop():
    msg, _ = cc.evaluate(HV, {}, "ok", NOW, login(12))
    assert "may stop working" in msg


def test_dead_probe_alerts_now_and_learns_a_shorter_lifetime():
    msg, entry = cc.evaluate(HV, {}, "dead", NOW, login(6))
    assert msg.startswith("\U0001f534") and "EXPIRED" in msg
    assert entry["lifetime_days"] == 6.0  # died at 6 days, not the assumed 11
    # next cycle reminds at ~4 days
    assert cc.evaluate(HV, {"lifetime_days": 6.0}, "ok", NOW, login(4.5))[0]
    assert cc.evaluate(HV, {"lifetime_days": 6.0}, "ok", NOW, login(3))[0] is None


def test_dead_after_full_lifetime_does_not_shrink_estimate():
    _, entry = cc.evaluate(HV, {}, "dead", NOW, login(13))
    assert entry["lifetime_days"] == 11


def test_unknown_probe_never_claims_expiry():
    msg, entry = cc.evaluate(HV, {}, "unknown", NOW, login(3))
    assert msg is None and entry["status"] == "unknown"


def test_fresh_login_rearms_reminders():
    _, entry = cc.evaluate(HV, {}, "ok", NOW, login(10))
    assert "last_alert_at" in entry
    _, entry = cc.evaluate(HV, entry, "ok", NOW + timedelta(days=1), login(0.1))
    assert "last_alert_at" not in entry


def test_run_end_to_end_with_fakes(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(cc, "authed_at", lambda key, entry: login(10 if key == "drive" else 1))
    sent = []
    probes = {k: (lambda: "ok") for k in cc.SYSTEMS}
    out = cc.run(probes=probes, now=NOW, send=lambda t: sent.append(t) or True)
    assert len(sent) == 1 and "Google Drive" in sent[0]  # 7-day lifetime, 10 days old
    assert any("-> message" in l for l in out)
    # second run the same day sends nothing new
    cc.run(probes=probes, now=NOW + timedelta(hours=2), send=lambda t: sent.append(t) or True)
    assert len(sent) == 1
    # dry run sends and saves nothing
    before = (tmp_path / "state.json").read_text()
    cc.run(probes=probes, now=NOW + timedelta(days=3), send=lambda t: sent.append(t) or True, dry_run=True)
    assert len(sent) == 1 and (tmp_path / "state.json").read_text() == before


def test_failed_delivery_is_retried_next_run(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(cc, "authed_at", lambda key, entry: login(10))
    probes = {k: (lambda: "ok") for k in cc.SYSTEMS}
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
