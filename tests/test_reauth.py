import json
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "hyvee"))

import reauth  # noqa: E402
import reauth_store  # noqa: E402


@pytest.fixture
def secret(tmp_path, monkeypatch):
    p = tmp_path / "secret.json"
    p.write_text(json.dumps({"installed": {
        "client_id": "cid", "client_secret": "sec", "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": ["http://localhost"]}}))
    monkeypatch.setenv("GOOGLE_DRIVE_CLIENT_SECRET", str(p))
    monkeypatch.setenv("GOOGLE_DRIVE_TOKEN", str(tmp_path / "token.json"))
    return p


def test_start_returns_url_and_saves_pending_verifier(secret, tmp_path):
    pend = tmp_path / "pending.json"
    out = reauth.start("drive", pending_path=pend)
    assert out["url"].startswith("https://accounts.google.com") and "code_challenge" in out["url"]
    saved = json.loads(pend.read_text())["drive"]
    assert saved["verifier"] and saved["state"]


def test_complete_rejects_garbage_and_missing_pending(secret, tmp_path):
    pend = tmp_path / "pending.json"
    with pytest.raises(reauth.ReauthError):
        reauth.complete("drive", "nothing here", pending_path=pend)
    with pytest.raises(reauth.ReauthError, match="No sign-in is in progress"):
        reauth.complete("drive", "http://localhost/callback?code=x&state=y", pending_path=pend)


def test_complete_expired_pending_is_dropped(secret, tmp_path):
    pend = tmp_path / "pending.json"
    pend.write_text(json.dumps({"drive": {"state": "s", "verifier": "v", "created": time.time() - 3600}}))
    with pytest.raises(reauth.ReauthError, match="No sign-in is in progress"):
        reauth.complete("drive", "http://localhost/callback?code=x&state=s", pending_path=pend)


def test_complete_exchanges_code_saves_token_and_clears_pending(secret, tmp_path, monkeypatch):
    import credential_check as cc
    monkeypatch.setattr(cc, "STATE_FILE", tmp_path / "cred.json")
    pend = tmp_path / "pending.json"
    reauth.start("drive", pending_path=pend)
    state = json.loads(pend.read_text())["drive"]["state"]

    def fake_fetch(self, token_url, authorization_response=None, **kw):
        assert "code=abc" in authorization_response
        assert kw.get("code_verifier")  # PKCE verifier restored from the pending file
        self.token = {"access_token": "at", "refresh_token": "rt", "token_type": "Bearer", "expires_in": 3600, "expires_at": time.time() + 3600}
        return self.token
    from requests_oauthlib import OAuth2Session
    with mock.patch.object(OAuth2Session, "fetch_token", fake_fetch):
        out = reauth.complete("drive", f"http://localhost/callback?code=abc&state={state}", pending_path=pend)
    assert out["has_refresh_token"]
    assert json.loads((tmp_path / "token.json").read_text())["refresh_token"] == "rt"
    assert "drive" not in json.loads(pend.read_text())
    assert "authed_at" in json.loads((tmp_path / "cred.json").read_text())["drive"]  # lifetime clock restarted


def test_unknown_system_and_cli_exit_codes(secret, capsys):
    assert reauth_store.main(["start", "nope"]) == 2
    assert "error" in capsys.readouterr().err


def test_down_alert_for_drive_mentions_reconnect(tmp_path):
    import system_health as sh
    sent = []
    for _ in range(2):
        sh.record("drive", "warn", "expired", db_path=tmp_path / "t.db", notify=sent.append)
    assert len(sent) == 1 and "reconnect" in sent[0].lower()


# ---- dashboard routes ----

def test_reconnect_pages_and_same_origin_guard(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from dashboard.app import app
    c = TestClient(app)
    page = c.get("/reconnect").text
    assert "Hy-Vee Login" in page and "paste cookies" not in page.lower()
    for gone in ("/reconnect/hyvee", "/reconnect/hyvee/start", "/reconnect/hyvee/cookies"):
        assert c.get(gone).status_code in (404, 405)
    assert c.post("/reconnect/drive/start", headers={"origin": "http://evil.example"}).status_code == 403
    r = c.post("/reconnect/drive/complete", data={"pasted": "junk"})
    assert r.status_code == 200 and "doesn" in r.text
