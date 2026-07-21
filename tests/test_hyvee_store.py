import json, os, sqlite3, subprocess, sys, tempfile, unittest
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

class TestResolve(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); self.tmp.close(); self.db = self.tmp.name
        run(["prefs","set","--item","milk","--preferred-product-id","111","--confidence","0.8"], self.db)
        run(["prefs","set","--item","eggs","--preferred-product-id","222","--confidence","0.5"], self.db)
    def tearDown(self): os.unlink(self.db)
    def _items(self, data):
        f = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w"); json.dump(data, f); f.close(); return f.name
    def test_decisions(self):
        path = self._items([{"item":"milk"},{"item":"eggs"},{"item":"kombucha"},
                            {"item":"soap","product_id":"999"}])
        out = run(["resolve","--items-json", path], self.db)
        d = {r["item"]: r["decision"] for r in out["resolved"]}
        self.assertEqual(d["milk"], "auto")
        self.assertEqual(d["eggs"], "flag")
        self.assertEqual(d["kombucha"], "search")
        self.assertEqual(d["soap"], "exact")

class TestRank(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); self.tmp.close(); self.db = self.tmp.name
    def tearDown(self): os.unlink(self.db)
    def _cands(self, data):
        f = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w"); json.dump(data, f); f.close(); return f.name
    def test_tiebreakers(self):
        cands = self._cands([
            {"product_id": "A", "name": "Prairie Farms Whole Milk", "price": "$3.49", "unit_price": "$6.98", "on_sale": False},
            {"product_id": "B", "name": "Hy-Vee Whole Milk", "price": "$2.99", "unit_price": "$5.98", "on_sale": False},
            {"product_id": "C", "name": "Kemps Whole Milk", "price": "$3.29", "unit_price": "$6.58", "on_sale": True},
        ])
        out = run(["rank", "--item", "milk", "--candidates-json", cands], self.db)
        order = [c["product_id"] for c in out]
        # C is on sale (beats non-sale); then cheaper unit price B before A;
        # B is also the Hy-Vee brand default.
        self.assertEqual(order[0], "C")
        self.assertEqual(order[1], "B")
        self.assertEqual(order[2], "A")

class TestRunLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); self.tmp.close(); self.db = self.tmp.name
        self.items_f = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
        self.resolved_f = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
    def tearDown(self):
        os.unlink(self.db)
        os.unlink(self.items_f.name)
        os.unlink(self.resolved_f.name)
    def test_run_log_persistence(self):
        json.dump([{"item": "milk"}], self.items_f); self.items_f.close()
        json.dump([{"item": "milk", "decision": "auto"}], self.resolved_f); self.resolved_f.close()
        out = run(["run", "log", "--items-json", self.items_f.name, "--resolved-json", self.resolved_f.name,
                   "--cart-verified", "1", "--summary", "test run"], self.db)
        self.assertIn("id", out)
        self.assertNotEqual(out["id"], "")
        rid = out["id"]
        conn = sqlite3.connect(self.db)
        row = conn.execute("SELECT items_json, resolved_json, cart_verified, summary FROM hyvee_cart_runs WHERE id=?", (rid,)).fetchone()
        self.assertIsNotNone(row, "Run not found in DB")
        items_data = json.loads(row[0])
        resolved_data = json.loads(row[1])
        self.assertEqual(len(items_data), 1)
        self.assertEqual(items_data[0]["item"], "milk")
        self.assertEqual(len(resolved_data), 1)
        self.assertEqual(resolved_data[0]["decision"], "auto")
        self.assertEqual(row[2], 1, "cart_verified should be 1")
        self.assertEqual(row[3], "test run", "summary mismatch")
        conn.close()

if __name__ == "__main__":
    unittest.main()
