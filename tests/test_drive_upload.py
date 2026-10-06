import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "scripts" / "drive_upload.py"


def run(args, token_path):
    env = {**os.environ, "GOOGLE_DRIVE_TOKEN": str(token_path)}
    return subprocess.run([sys.executable, str(CLI), *args],
                          capture_output=True, text=True, env=env)


class TestDriveUploadCli(unittest.TestCase):
    def setUp(self):
        # a token path that does not exist -> unauthorized
        self.token = Path(tempfile.gettempdir()) / "no_such_drive_token_xyz.json"
        if self.token.exists():
            self.token.unlink()

    def test_status_without_token_reports_actionable_error(self):
        p = run(["status"], self.token)
        out = json.loads(p.stdout or p.stderr)
        self.assertIn("error", out)
        self.assertIn("auth", out["error"].lower())

    def test_upload_missing_file_reports_error(self):
        p = run(["upload", "--file", "does_not_exist.xlsx", "--parent", "X"], self.token)
        out = json.loads(p.stdout or p.stderr)
        self.assertIn("error", out)
        self.assertIn("file not found", out["error"].lower())

    def test_upload_existing_file_without_token_hits_auth_error(self):
        # __file__ exists, so we get past the file check to the auth check
        p = run(["upload", "--file", str(CLI), "--parent", "X"], self.token)
        out = json.loads(p.stdout or p.stderr)
        self.assertIn("error", out)
        self.assertIn("token", out["error"].lower())


if __name__ == "__main__":
    unittest.main()


def test_dead_refresh_token_falls_through_to_consent_flow(monkeypatch, tmp_path):
    import sys
    from pathlib import Path
    from unittest import mock

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import drive_upload as du
    from google.auth.exceptions import RefreshError

    token = tmp_path / "token.json"
    token.write_text("{}")
    secret = tmp_path / "secret.json"
    secret.write_text("{}")
    monkeypatch.setenv("GOOGLE_DRIVE_TOKEN", str(token))
    monkeypatch.setenv("GOOGLE_DRIVE_CLIENT_SECRET", str(secret))

    dead = mock.Mock(valid=False, expired=True, refresh_token="r")
    dead.refresh.side_effect = RefreshError("invalid_grant")
    fresh = mock.Mock()
    fresh.to_json.return_value = '{"fresh": true}'
    flow = mock.Mock()
    flow.run_local_server.return_value = fresh

    with mock.patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=dead), \
         mock.patch("google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file", return_value=flow):
        # Unattended callers get an actionable error, not a raw RefreshError.
        try:
            du._load_credentials(interactive=False)
            raise AssertionError("expected RuntimeError")
        except RuntimeError as exc:
            assert "drive_upload.py auth" in str(exc)
        # `auth` discards the dead token and runs the consent flow.
        assert du._load_credentials(interactive=True) is fresh
    assert token.read_text() == '{"fresh": true}'
    assert flow.run_local_server.call_args.kwargs["open_browser"] is False
