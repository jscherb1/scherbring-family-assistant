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

class TestHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); self.tmp.close()
        self.db = self.tmp.name
        self.j = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w");
        json.dump({"purchaseGroups": [{"title": "July 2026", "purchaseCards": [
            {"purchaseId": "o1", "date": "2026-07-19", "summary": "$5 - picked up",
             "items": [{"image": {"url": "https://cdn/products/00123/CF/x.jpg", "altText": "Kemps Whole Milk"}}]},
            {"purchaseId": "o2", "date": "2026-06-10", "summary": "$5", "items": [
             {"image": {"url": "https://cdn/products/00123/CF/x.jpg", "altText": "Kemps Whole Milk"}}]}]}]},
            self.j); self.j.close()
    def tearDown(self):
        os.unlink(self.db); os.unlink(self.j.name)
    def test_ingest_and_stats(self):
        res = run(["history", "ingest", "--json", self.j.name], self.db)
        self.assertEqual(res["inserted"], 2)
        run(["history", "ingest", "--json", self.j.name], self.db)  # idempotent
        stats = run(["history", "stats", "--item", "milk"], self.db)
        top = stats[0]
        self.assertEqual(top["product_name"], "Kemps Whole Milk")
        self.assertEqual(top["times"], 2)
        self.assertEqual(top["last_order_date"], "2026-07-19")
        self.assertEqual(top["upc"], "00123")

class TestPrefs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); self.tmp.close(); self.db = self.tmp.name
    def tearDown(self): os.unlink(self.db)
    def test_set_get_upsert(self):
        run(["prefs", "set", "--item", "milk", "--preferred-product-id", "4160380",
             "--product-name", "Kemps Whole Milk", "--confidence", "0.8"], self.db)
        got = run(["prefs", "get", "--item", "milk"], self.db)
        self.assertEqual(got["preferred_product_id"], "4160380")
        self.assertAlmostEqual(got["confidence"], 0.8)
        run(["prefs", "set", "--item", "milk", "--confidence", "0.9"], self.db)  # upsert keeps product
        got = run(["prefs", "get", "--item", "milk"], self.db)
        self.assertEqual(got["preferred_product_id"], "4160380")
        self.assertAlmostEqual(got["confidence"], 0.9)

class TestFeedback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); self.tmp.close(); self.db = self.tmp.name
        run(["prefs", "set", "--item", "milk", "--preferred-product-id", "111",
             "--confidence", "0.7"], self.db)
    def tearDown(self): os.unlink(self.db)
    def test_accept_raises_and_graduates(self):
        out = run(["feedback", "record", "--item", "milk", "--action", "accepted",
                   "--proposed-product-id", "111"], self.db)
        self.assertAlmostEqual(out["pref"]["confidence"], 0.8)
        self.assertTrue(out["auto_add"])
        self.assertEqual(out["pref"]["times_confirmed"], 1)
    def test_substitute_switches_product_and_lowers(self):
        out = run(["feedback", "record", "--item", "milk", "--action", "substituted",
                   "--proposed-product-id", "111", "--chosen-product-id", "222"], self.db)
        self.assertEqual(out["pref"]["preferred_product_id"], "222")
        self.assertAlmostEqual(out["pref"]["confidence"], 0.5)
        self.assertFalse(out["auto_add"])
        self.assertEqual(out["pref"]["times_rejected"], 1)
    def test_feedback_creates_pref_when_absent(self):
        out = run(["feedback", "record", "--item", "cheese", "--action", "accepted",
                   "--proposed-product-id", "333"], self.db)
        self.assertTrue("pref" in out)
        self.assertAlmostEqual(out["pref"]["confidence"], 0.1)
        self.assertEqual(out["pref"]["times_confirmed"], 1)
        self.assertFalse(out["auto_add"])

class TestSeed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); self.tmp.close()
        self.db = self.tmp.name
        self.j = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
        json.dump({"purchaseGroups": [{"title": "July 2026", "purchaseCards": [
            {"purchaseId": "o1", "date": "2026-07-19", "summary": "$5 - picked up",
             "items": [{"image": {"url": "https://cdn/products/00123/CF/x.jpg", "altText": "Kemps Whole Milk"}}]},
            {"purchaseId": "o2", "date": "2026-06-10", "summary": "$5", "items": [
             {"image": {"url": "https://cdn/products/00123/CF/x.jpg", "altText": "Kemps Whole Milk"}}]}]}]},
            self.j); self.j.close()
        run(["history", "ingest", "--json", self.j.name], self.db)
    def tearDown(self):
        os.unlink(self.db); os.unlink(self.j.name)
    def test_seed_from_history(self):
        out = run(["seed", "--items", "milk,ranch"], self.db)
        seeded = {s["item"]: s for s in out}
        self.assertIn("milk", seeded)
        self.assertEqual(seeded["milk"]["product_name"], "Kemps Whole Milk")
        self.assertAlmostEqual(seeded["milk"]["confidence"], 0.6)  # 0.4 + 0.1*2
        self.assertNotIn("ranch", seeded)  # no history match -> skipped

if __name__ == "__main__":
    unittest.main()
