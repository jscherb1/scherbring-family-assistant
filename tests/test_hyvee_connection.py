import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "hyvee"))

import hyvee_connection as hc  # noqa: E402
import system_health as sh  # noqa: E402


class FakeResp:
    def __init__(self, status, body=None):
        self.status, self._body = status, body

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class FakePage:
    def __init__(self, status, body=None, title="Hy-Vee", url="https://www.hy-vee.com/"):
        self.url, self._title = url, title
        self.request = type("R", (), {"get": lambda _s, _u: FakeResp(status, body)})()

    def goto(self, *a, **k): pass
    def wait_for_timeout(self, *a): pass
    def title(self): return self._title


@pytest.mark.parametrize("status,body,title,want", [
    (200, {"purchaseGroups": []}, "Hy-Vee", "ok"),
    (401, None, "Hy-Vee", "expired"),
    (403, None, "Hy-Vee", "expired"),
    (429, None, "Hy-Vee", "unknown"),
    (503, None, "Hy-Vee", "unknown"),
    (200, {"other": 1}, "Hy-Vee", "unknown"),
    (200, None, "Hy-Vee", "unknown"),
    (403, None, "Just a moment...", "unknown"),  # Cloudflare challenge is never "expired"
])
def test_probe_page_three_way(status, body, title, want):
    res = hc.probe_page(FakePage(status, body, title))
    assert res["status"] == want and res["reason"]


def test_probe_page_exception_is_unknown():
    class Boom(FakePage):
        def goto(self, *a, **k): raise RuntimeError("net")
    assert hc.probe_page(Boom(200))["status"] == "unknown"


def _res(status): return hc.result(status, f"r-{status}")


def test_missing_session_is_expired(tmp_path):
    assert hc.check(tmp_path / "none.json")["status"] == "expired"


def test_expired_is_retested_and_a_flip_is_not_believed(tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "CHECK_LOG", tmp_path / "log.jsonl")
    f = tmp_path / "s.json"; f.write_text("{}")
    seq = iter([_res("expired"), _res("ok")])
    res = hc.check(f, probe=lambda *_: next(seq), sleep=lambda _: None)
    assert res["status"] == "unknown" and "re-test said ok" in res["reason"]
    both = iter([_res("expired"), _res("expired")])
    assert hc.check(f, probe=lambda *_: next(both), sleep=lambda _: None)["status"] == "expired"


def test_every_check_is_logged(tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "CHECK_LOG", tmp_path / "log.jsonl")
    f = tmp_path / "s.json"; f.write_text("{}")
    hc.check(f, probe=lambda *_: _res("ok"), sleep=lambda _: None)
    line = json.loads((tmp_path / "log.jsonl").read_text().splitlines()[0])
    assert line["status"] == "ok"


def test_health_mapping_says_why_and_how_to_fix():
    assert hc.health_detail(_res("ok"))[0] == "ok"
    st, detail = hc.health_detail(hc.result("expired", "Hy-Vee rejected the saved session (order-history API 401)"))
    assert st == "down" and "401" in detail and "Hy-Vee Login" in detail
    assert hc.health_detail(_res("unknown"))[0] == "unknown"


def test_unknown_never_flips_health_or_alerts(tmp_path):
    db, sent = tmp_path / "t.db", []
    sh.record("hyvee", "ok", "fine", db_path=db, notify=sent.append)
    sh.record("hyvee", "unknown", "cloudflare", db_path=db, notify=sent.append)
    row = next(s for s in sh.get_status(db_path=db) if s["key"] == "hyvee")
    assert row["status"] == "ok" and not sent


def test_expired_alerts_once_with_reason_and_recovers(tmp_path):
    db, sent = tmp_path / "t.db", []
    st, detail = hc.health_detail(hc.result("expired", "rejected (order-history API 401)"))
    sh.record("hyvee", st, detail, db_path=db, notify=sent.append)
    sh.record("hyvee", st, detail, db_path=db, notify=sent.append)  # still down: no second message
    assert len(sent) == 1 and "401" in sent[0] and "Hy-Vee Login" in sent[0]
    sh.record("hyvee", "ok", "signed in", db_path=db, notify=sent.append)
    assert len(sent) == 2 and "working again" in sent[1]


def test_on_login_saved_records_time_and_runs_the_one_check(tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "STATE_FILE", tmp_path / "conn.json")
    calls = []
    monkeypatch.setattr(sh, "run_checks", lambda keys, **kw: calls.append(keys) or [{"status": "ok", "detail": "signed in"}])
    out = hc.on_login_saved("login_test", db_path=tmp_path / "t.db")
    assert calls == [["hyvee"]] and out["status"] == "ok"
    assert json.loads((tmp_path / "conn.json").read_text())["source"] == "login_test"
    assert hc.login_age_days() is not None and hc.login_age_days() < 0.01


def test_credential_check_no_longer_owns_hyvee():
    import credential_check as cc
    assert "hyvee" not in cc.SYSTEMS and "hyvee" not in cc.PROBES
