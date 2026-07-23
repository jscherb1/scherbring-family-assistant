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
