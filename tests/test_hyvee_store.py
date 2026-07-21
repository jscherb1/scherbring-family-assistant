import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STORE = REPO / "scripts" / "hyvee_store.py"

def run(args, db):
    env = {**os.environ, "HYVEE_DB_PATH": str(db)}
    p = subprocess.run([sys.executable, str(STORE), *args],
                       capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout) if p.stdout.strip() else None

class TestSchema(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close(); self.db = self.tmp.name
    def tearDown(self):
        os.unlink(self.db)
    def test_prefs_list_empty_ok(self):
        out = run(["prefs", "list"], self.db)
        self.assertEqual(out, [])

if __name__ == "__main__":
    unittest.main()
