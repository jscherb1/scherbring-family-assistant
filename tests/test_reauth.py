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
import remote_login  # noqa: E402


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


def test_complete_exchanges_code_saves_token_and_clears_pending(secret, tmp_path):
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


def test_unknown_system_and_cli_exit_codes(secret, capsys):
    assert reauth_store.main(["start", "nope"]) == 2
    assert "error" in capsys.readouterr().err


def test_down_alert_for_drive_mentions_reconnect(tmp_path):
    import system_health as sh
    sent = []
    for _ in range(2):
        sh.record("drive", "warn", "expired", db_path=tmp_path / "t.db", notify=sent.append)
    assert len(sent) == 1 and "reconnect" in sent[0].lower()


# ---- Hy-Vee remote browser (fake page, no real browser) ----

class FakePage:
    def __init__(self):
        self.url = "https://identity.hy-vee.com/login"
        self.clicked, self.typed = [], []
        self.mouse = mock.Mock(click=lambda x, y: self.clicked.append((x, y)))
        self.keyboard = mock.Mock(type=lambda t, delay=0: self.typed.append(t), press=lambda k: self.typed.append(k))

    def screenshot(self, **kw):
        return b"\xff\xd8jpeg"


def test_remote_login_click_type_and_finish_saves_session(tmp_path, monkeypatch):
    import login_test
    page = FakePage()
    ctx = mock.Mock()
    ctx.storage_state = lambda path: Path(path).write_text("{}")
    launcher = lambda p: (mock.Mock(), ctx, page)  # noqa: E731
    session_file = tmp_path / "hv.json"
    monkeypatch.setattr(login_test, "is_logged_in", lambda pg: True)
    rl = remote_login.RemoteLogin(session_file=session_file, launcher=launcher)
    # the launcher path skips real Playwright, but _run still imports playwright's sync_playwright
    rl.start()
    for _ in range(50):
        if rl.status()["phase"] == "waiting":
            break
        time.sleep(0.1)
    assert rl.screenshot().startswith(b"\xff\xd8")
    rl.click(10, 20)
    rl.type_text("hello")
    rl.press("Enter")
    assert page.clicked == [(10.0, 20.0)] and page.typed == ["hello", "Enter"]
    with pytest.raises(remote_login.RemoteLoginError):
        rl.press("Delete")
    rl.finish()  # still on the identity host: not signed in yet
    assert not session_file.exists()
    page.url = "https://www.hy-vee.com/"
    rl.finish()
    assert session_file.exists() and rl.status()["phase"] == "done"


def test_remote_login_requires_running_session():
    rl = remote_login.RemoteLogin()
    with pytest.raises(remote_login.RemoteLoginError):
        rl.click(1, 1)


# ---- dashboard routes ----

def test_reconnect_pages_and_same_origin_guard(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from dashboard.app import app
    c = TestClient(app)
    assert c.get("/reconnect").status_code == 200
    assert "Reconnect Hy-Vee" in c.get("/reconnect/hyvee").text
    assert c.get("/reconnect/hyvee/shot.jpg").status_code == 204  # no session running
    bad = {"origin": "http://evil.example"}
    for path in ("/reconnect/drive/start", "/reconnect/hyvee/start", "/reconnect/hyvee/click",
                 "/reconnect/hyvee/type", "/reconnect/hyvee/finish"):
        assert c.post(path, headers=bad, data={"x": 1, "y": 1}).status_code == 403
    r = c.post("/reconnect/hyvee/click", data={"x": 1, "y": 1})
    assert r.status_code == 400 and "No sign-in session" in r.json()["error"]
    r = c.post("/reconnect/drive/complete", data={"pasted": "junk"})
    assert r.status_code == 200 and "doesn" in r.text
