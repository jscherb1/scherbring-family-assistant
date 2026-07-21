# Hy-Vee Cart Builder — Decision Model & Learning Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. On execution, also copy this plan to `docs/superpowers/plans/2026-07-21-hyvee-cart-builder.md` (plan-mode restricted edits to this file).

**Goal:** Turn a Todoist shopping list into a built-but-never-placed Hy-Vee cart, resolving each item to a specific product using purchase history + learned preferences, and improving from user feedback.

**Architecture:** A zero-dependency `scripts/hyvee_store.py` CLI owns all data/decision logic (SQLite tables in the shared `state/agent_results.db`); the existing `scripts/hyvee/` Playwright layer gains commands to sync history, search, add-to-cart and verify the cart via Hy-Vee's JSON APIs; a new `.claude/agents/hyvee.md` subagent orchestrates. The meal-planner/todoist agents adopt a shared structured item format so detail is captured up front.

**Tech Stack:** Python 3.13 stdlib (store + tests via `unittest`), Playwright (browser layer, already installed), SQLite, Todoist MCP.

## Context

Phase 1 proved autonomous login + add-to-cart (`scripts/hyvee/login_test.py`). The discovery spike (`docs/superpowers/specs/2026-07-21-hyvee-data-discovery.md`) mapped Hy-Vee's data: paginated purchase-history JSON API, a getActiveCart GraphQL endpoint, and per-product fields (productId, UPC, name, size, price, unit price, sale, promo badges, isBuyAgain, sponsored). Brand is not structured, so we remember products by productId/UPC. Full design: `docs/superpowers/specs/2026-07-21-hyvee-cart-builder-decision-model-design.md`. This plan builds the decision/learning layer and the agent contract on top of Phase 1.

## Global Constraints

- **Model policy:** any AI config (the `hyvee` subagent frontmatter) MUST set `model: sonnet` (repo CLAUDE.md). Never Opus without explicit user approval.
- **Build-only:** the flow NEVER navigates to checkout or places an order, in this phase or any future phase.
- **Store is zero-dependency:** `scripts/hyvee_store.py` uses Python stdlib only (argparse, sqlite3, json, uuid, datetime). Tests use stdlib `unittest`. No pytest/pip deps for the store.
- **Store conventions (match existing `scripts/*_store.py`):** `_connect()` runs `SCHEMA_PATH.read_text()` via `executescript` on every connect (idempotent `CREATE TABLE IF NOT EXISTS`); every command prints JSON to stdout via `json.dumps(obj, ensure_ascii=False, indent=2)`; ids are `uuid.uuid4().hex`; timestamps are ISO-8601. Reconfigure stdout/stderr to utf-8 as the other stores do.
- **Test isolation:** `hyvee_store.py` resolves its DB path from env var `HYVEE_DB_PATH` if set, else the default `state/agent_results.db`. Tests set `HYVEE_DB_PATH` to a temp file.
- **Browser layer:** reuse `state/hyvee_session.json`, dismiss OneTrust (`#onetrust-accept-btn-handler`), exclude `[data-testid='sponsored-text']` results, cart count from `[data-testid='global-navigation-cart-bubble']`.

## File Structure

- Create `state/schema.sql` additions — 4 new tables (see Task 1).
- Create `scripts/hyvee_store.py` — the decision/data CLI (Tasks 1–6).
- Create `tests/test_hyvee_store.py` — stdlib unittest suite (Tasks 1–6).
- Create `scripts/hyvee/hyvee_web.py` — the single source of truth for all site selectors, URLs, and API endpoints the automation depends on (Task 7). Every other browser script imports from here so a site change is a one-file fix.
- Create `scripts/hyvee/diagnose.py` — reusable diagnostics CLI to adapt to site changes: element/testid inventory, network-API capture, product-card structure dump, and a `check` health-check of every selector/endpoint the system relies on (Task 7). Promotes the throwaway discovery probes into a maintained tool.
- Create `scripts/hyvee/cart_ops.py` — Playwright commands: sync_history, search, add_to_cart, verify_cart, importing all selectors/endpoints from `hyvee_web.py` (Task 8).
- Retire `scripts/hyvee/explore.py` — its capabilities are absorbed into `diagnose.py`; delete it in Task 7.
- Create `.claude/agents/hyvee.md` — orchestrating subagent (Task 9).
- Modify `.claude/agents/meal-planner.md` and `.claude/agents/todoist.md` — adopt item format (Task 10).

---

### Task 0: Branch + commit existing work

The Phase-1 browser test, the two spec docs, the discovery doc, and this plan are currently untracked on `main`. Isolate the feature and commit them before building.

- [ ] **Step 1: Create the feature branch**

```bash
git checkout -b hyvee-cart-builder
```

- [ ] **Step 2: Commit Phase-1 code + config changes**

```bash
git add scripts/hyvee/requirements.txt scripts/hyvee/login_test.py scripts/hyvee/explore.py .env.example .gitignore
git commit -m "feat(hyvee): phase-1 login + add-to-cart automation (Playwright)"
```

- [ ] **Step 3: Commit the design/discovery/plan docs**

```bash
git add docs/superpowers/specs/2026-07-21-hyvee-login-addtocart-test-design.md \
        docs/superpowers/specs/2026-07-21-hyvee-data-discovery.md \
        docs/superpowers/specs/2026-07-21-hyvee-cart-builder-decision-model-design.md
mkdir -p docs/superpowers/plans && cp "$(cygpath -u "$USERPROFILE")/.claude/plans/can-we-start-work-toasty-cake.md" docs/superpowers/plans/2026-07-21-hyvee-cart-builder.md
git add docs/superpowers/plans/2026-07-21-hyvee-cart-builder.md
git commit -m "docs(hyvee): cart-builder design, data discovery, and implementation plan"
```

Note: confirm `.gitignore` keeps `state/hyvee_session.json` and `scripts/hyvee/last_error.png` out (verified in Phase 1). Never commit `.env` or the session file.

---

### Task 1: Schema tables + store skeleton

**Files:**
- Modify: `state/schema.sql` (append 4 tables)
- Create: `scripts/hyvee_store.py`
- Test: `tests/test_hyvee_store.py`

**Interfaces:**
- Produces: `_connect() -> sqlite3.Connection`; `_db_path() -> Path` (honors `HYVEE_DB_PATH`); `_out(obj)` prints JSON; `_now() -> str` ISO timestamp. CLI entrypoint `main(argv)`.

- [ ] **Step 1: Append schema (append to `state/schema.sql`)**

```sql
-- Hy-Vee cart builder: synced purchase-history snapshot (frequency/recency source).
CREATE TABLE IF NOT EXISTS hyvee_purchase_history (
    id           TEXT PRIMARY KEY,      -- uuid4 hex
    upc          TEXT,                  -- from product image URL (may be null)
    product_name TEXT NOT NULL,         -- from image altText
    order_date   TEXT NOT NULL,         -- ISO date (YYYY-MM-DD)
    purchase_id  TEXT NOT NULL,         -- Hy-Vee order uuid
    synced_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_hyvee_history_unique
    ON hyvee_purchase_history (purchase_id, product_name);
CREATE INDEX IF NOT EXISTS idx_hyvee_history_name
    ON hyvee_purchase_history (product_name, order_date DESC);

-- Learned item->product map, one row per normalized item key (e.g. "milk").
CREATE TABLE IF NOT EXISTS hyvee_item_prefs (
    item                 TEXT PRIMARY KEY,   -- normalized key
    preferred_product_id TEXT,
    preferred_upc        TEXT,
    product_name         TEXT,
    size                 TEXT,
    confidence           REAL NOT NULL DEFAULT 0.0,  -- 0.0..1.0
    pref_brand           TEXT,
    max_price            REAL,
    prefer_on_sale       INTEGER NOT NULL DEFAULT 0, -- 0/1
    pref_size            TEXT,
    source               TEXT NOT NULL DEFAULT 'history', -- history|user|mealplan
    times_confirmed      INTEGER NOT NULL DEFAULT 0,
    times_rejected       INTEGER NOT NULL DEFAULT 0,
    updated_at           TEXT NOT NULL
);

-- Append-only feedback driving confidence changes (auditable).
CREATE TABLE IF NOT EXISTS hyvee_feedback_log (
    id                  TEXT PRIMARY KEY,   -- uuid4 hex
    ts                  TEXT NOT NULL,
    item                TEXT NOT NULL,
    proposed_product_id TEXT,
    action              TEXT NOT NULL,      -- accepted|rejected|substituted
    chosen_product_id   TEXT,
    note                TEXT
);
CREATE INDEX IF NOT EXISTS idx_hyvee_feedback_item ON hyvee_feedback_log (item, ts DESC);

-- Per cart-build run log.
CREATE TABLE IF NOT EXISTS hyvee_cart_runs (
    id            TEXT PRIMARY KEY,   -- uuid4 hex
    ts            TEXT NOT NULL,
    items_json    TEXT NOT NULL,      -- list of parsed input items
    resolved_json TEXT NOT NULL,      -- item -> product + auto/flagged
    cart_verified INTEGER NOT NULL DEFAULT 0,
    summary       TEXT
);
```

- [ ] **Step 2: Write the failing test for skeleton + schema init**

```python
# tests/test_hyvee_store.py
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
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m unittest tests.test_hyvee_store -v`
Expected: FAIL (hyvee_store.py does not exist).

- [ ] **Step 4: Write the store skeleton**

```python
#!/usr/bin/env python3
"""Hy-Vee cart-builder store CLI for the hyvee subagent (stdlib only).

Owns purchase-history snapshot, learned item->product prefs, feedback log, and
cart-run log in the shared SQLite DB (state/agent_results.db, schema in
state/schema.sql). All commands print JSON to stdout. No browser logic here.
"""
from __future__ import annotations
import argparse, json, os, sqlite3, sys, uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "state" / "schema.sql"

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8")

def _db_path() -> Path:
    env = os.environ.get("HYVEE_DB_PATH")
    return Path(env) if env else REPO_ROOT / "state" / "agent_results.db"

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))

def cmd_prefs_list(args) -> None:
    conn = _connect()
    rows = conn.execute("SELECT * FROM hyvee_item_prefs ORDER BY item").fetchall()
    _out([dict(r) for r in rows])

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="group", required=True)
    prefs = sub.add_parser("prefs").add_subparsers(dest="action", required=True)
    prefs.add_parser("list").set_defaults(func=cmd_prefs_list)
    args = ap.parse_args(argv)
    args.func(args)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m unittest tests.test_hyvee_store -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add state/schema.sql scripts/hyvee_store.py tests/test_hyvee_store.py
git commit -m "feat(hyvee): add cart-builder schema tables and store skeleton"
```

---

### Task 2: Purchase-history ingest

**Files:**
- Modify: `scripts/hyvee_store.py`
- Test: `tests/test_hyvee_store.py`

**Interfaces:**
- Produces: CLI `history ingest --json <path>` (reads a purchase-history API response file, upserts rows, dedup via unique index) and `history stats [--item milk]` (returns per-product-name frequency + last order_date).

- [ ] **Step 1: Write failing test**

```python
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
```

- [ ] **Step 2: Run test, verify FAIL** — `python -m unittest tests.test_hyvee_store.TestHistory -v` → FAIL (no such command).

- [ ] **Step 3: Implement ingest + stats**

```python
import re

def _extract_upc(url: str):
    m = re.search(r"/products/(\d+)/", url or "")
    return m.group(1) if m else None

def cmd_history_ingest(args) -> None:
    data = json.loads(Path(args.json).read_text(encoding="utf-8"))
    conn = _connect(); inserted = 0
    for grp in data.get("purchaseGroups", []):
        for card in grp.get("purchaseCards", []):
            pid = card.get("purchaseId"); date = card.get("date")
            for it in (card.get("items") or []):
                img = it.get("image") or {}
                name = (img.get("altText") or "").strip()
                if not name:
                    continue
                try:
                    conn.execute(
                        "INSERT INTO hyvee_purchase_history "
                        "(id, upc, product_name, order_date, purchase_id, synced_at) "
                        "VALUES (?,?,?,?,?,?)",
                        (uuid.uuid4().hex, _extract_upc(img.get("url")), name, date, pid, _now()))
                    inserted += 1
                except sqlite3.IntegrityError:
                    pass  # dedup on (purchase_id, product_name)
    conn.commit()
    _out({"inserted": inserted})

def cmd_history_stats(args) -> None:
    conn = _connect(); params = []; where = ""
    if args.item:
        where = "WHERE lower(product_name) LIKE ?"; params.append(f"%{args.item.lower()}%")
    rows = conn.execute(
        f"SELECT product_name, COUNT(*) AS times, MAX(order_date) AS last_order_date, "
        f"MAX(upc) AS upc FROM hyvee_purchase_history {where} "
        f"GROUP BY product_name ORDER BY times DESC, last_order_date DESC", params).fetchall()
    _out([dict(r) for r in rows])
```

Wire subparsers in `main`:

```python
    history = sub.add_parser("history").add_subparsers(dest="action", required=True)
    hi = history.add_parser("ingest"); hi.add_argument("--json", required=True)
    hi.set_defaults(func=cmd_history_ingest)
    hs = history.add_parser("stats"); hs.add_argument("--item", default=None)
    hs.set_defaults(func=cmd_history_stats)
```

- [ ] **Step 4: Run test, verify PASS.**

- [ ] **Step 5: Commit** — `git commit -am "feat(hyvee): purchase-history ingest and per-item stats"`

---

### Task 3: Item prefs CRUD + item normalization

**Files:** Modify `scripts/hyvee_store.py`; Test `tests/test_hyvee_store.py`

**Interfaces:**
- Produces: `normalize_item(text) -> str` (lowercased, strips quantities/punctuation/leading counts, singularizes trailing 's' for simple plurals); CLI `prefs set --item <k> [--preferred-product-id ...] [--preferred-upc ...] [--product-name ...] [--size ...] [--confidence f] [--pref-brand ...] [--max-price f] [--prefer-on-sale 0|1] [--pref-size ...] [--source ...]` (upsert), `prefs get --item <k>`.

- [ ] **Step 1: Failing test**

```python
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
```

- [ ] **Step 2: Run, verify FAIL.**

- [ ] **Step 3: Implement**

```python
def normalize_item(text: str) -> str:
    t = (text or "").lower().strip()
    t = re.sub(r"^\s*\d+\s*(x|ct|count|pk|pack|lb|lbs|oz|gal)?\s*", "", t)  # leading qty
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if t.endswith("s") and not t.endswith("ss"):
        t = t[:-1]
    return t

_PREF_FIELDS = ["preferred_product_id", "preferred_upc", "product_name", "size",
                "confidence", "pref_brand", "max_price", "prefer_on_sale", "pref_size", "source"]

def cmd_prefs_set(args) -> None:
    item = normalize_item(args.item)
    conn = _connect()
    row = conn.execute("SELECT * FROM hyvee_item_prefs WHERE item=?", (item,)).fetchone()
    vals = {f: getattr(args, f) for f in _PREF_FIELDS}
    if row is None:
        cols = ["item"] + _PREF_FIELDS + ["updated_at"]
        data = [item] + [vals[f] for f in _PREF_FIELDS] + [_now()]
        # defaults for NOT NULL columns when omitted
        conn.execute(f"INSERT INTO hyvee_item_prefs ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})",
                     [(_pref_default(f, v)) for f, v in zip(cols, data)])
    else:
        sets, params = [], []
        for f in _PREF_FIELDS:
            if vals[f] is not None:
                sets.append(f"{f}=?"); params.append(vals[f])
        sets.append("updated_at=?"); params.append(_now()); params.append(item)
        conn.execute(f"UPDATE hyvee_item_prefs SET {','.join(sets)} WHERE item=?", params)
    conn.commit()
    _out(dict(conn.execute("SELECT * FROM hyvee_item_prefs WHERE item=?", (item,)).fetchone()))

def _pref_default(col, v):
    if v is not None:
        return v
    return {"confidence": 0.0, "prefer_on_sale": 0, "times_confirmed": 0,
            "times_rejected": 0, "source": "history"}.get(col, None)

def cmd_prefs_get(args) -> None:
    conn = _connect()
    row = conn.execute("SELECT * FROM hyvee_item_prefs WHERE item=?", (normalize_item(args.item),)).fetchone()
    _out(dict(row) if row else {})
```

Wire subparsers (add typed args: `--confidence`/`--max-price` as `type=float`, `--prefer-on-sale` as `type=int`).

- [ ] **Step 4: Run, verify PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(hyvee): item-prefs upsert/get with item normalization"`

---

### Task 4: Confidence math + feedback application

**Files:** Modify `scripts/hyvee_store.py`; Test `tests/test_hyvee_store.py`

**Interfaces:**
- Produces: constants `AUTO_THRESHOLD=0.75`, `STEP_UP=0.1`, `STEP_DOWN=0.2`; CLI `feedback record --item <k> --action accepted|rejected|substituted [--proposed-product-id ...] [--chosen-product-id ...] [--note ...]` which logs to `hyvee_feedback_log` AND adjusts the pref row (confidence clamp 0..1, bump counters; on `substituted` set `preferred_product_id=chosen`, on `accepted` confidence+=STEP_UP, on `rejected`/`substituted` confidence-=STEP_DOWN). Returns the updated pref + `auto_add` bool (`confidence >= AUTO_THRESHOLD`).

- [ ] **Step 1: Failing test**

```python
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
```

- [ ] **Step 2: Run, verify FAIL.**

- [ ] **Step 3: Implement**

```python
AUTO_THRESHOLD = 0.75
STEP_UP = 0.1
STEP_DOWN = 0.2

def _clamp(x): return max(0.0, min(1.0, x))

def cmd_feedback_record(args) -> None:
    item = normalize_item(args.item)
    conn = _connect()
    conn.execute("INSERT INTO hyvee_feedback_log (id, ts, item, proposed_product_id, action, chosen_product_id, note) "
                 "VALUES (?,?,?,?,?,?,?)",
                 (uuid.uuid4().hex, _now(), item, args.proposed_product_id, args.action,
                  args.chosen_product_id, args.note))
    row = conn.execute("SELECT * FROM hyvee_item_prefs WHERE item=?", (item,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO hyvee_item_prefs (item, confidence, updated_at) VALUES (?,?,?)",
                     (item, 0.0, _now()))
        row = conn.execute("SELECT * FROM hyvee_item_prefs WHERE item=?", (item,)).fetchone()
    conf = row["confidence"]; tc = row["times_confirmed"]; tr = row["times_rejected"]
    ppid = row["preferred_product_id"]
    if args.action == "accepted":
        conf = _clamp(conf + STEP_UP); tc += 1
    elif args.action == "rejected":
        conf = _clamp(conf - STEP_DOWN); tr += 1
    elif args.action == "substituted":
        conf = _clamp(conf - STEP_DOWN); tr += 1
        if args.chosen_product_id:
            ppid = args.chosen_product_id
    conn.execute("UPDATE hyvee_item_prefs SET confidence=?, times_confirmed=?, times_rejected=?, "
                 "preferred_product_id=?, updated_at=? WHERE item=?",
                 (conf, tc, tr, ppid, _now(), item))
    conn.commit()
    pref = dict(conn.execute("SELECT * FROM hyvee_item_prefs WHERE item=?", (item,)).fetchone())
    _out({"pref": pref, "auto_add": conf >= AUTO_THRESHOLD})
```

Wire the `feedback record` subparser.

- [ ] **Step 4: Run, verify PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(hyvee): feedback-driven confidence learning"`

---

### Task 5: Seed prefs from history

**Files:** Modify `scripts/hyvee_store.py`; Test `tests/test_hyvee_store.py`

**Interfaces:**
- Produces: CLI `seed --items milk,eggs,bread [--min-confidence 0.0]`. For each item key, reads `history stats` for that item, picks the top product (most times, then most recent), and upserts a pref with `source=history` and a seed confidence = `min(0.9, 0.4 + 0.1*times)` (so 1 buy→0.5, 5+ buys→0.9). Skips items with no history match. Returns list of seeded prefs.

- [ ] **Step 1: Failing test** (ingest two milk orders as in Task 2, then):

```python
class TestSeed(unittest.TestCase):
    # setUp ingests the same fixture JSON as TestHistory into self.db
    def test_seed_from_history(self):
        out = run(["seed", "--items", "milk,ranch"], self.db)
        seeded = {s["item"]: s for s in out}
        self.assertIn("milk", seeded)
        self.assertEqual(seeded["milk"]["product_name"], "Kemps Whole Milk")
        self.assertAlmostEqual(seeded["milk"]["confidence"], 0.6)  # 0.4 + 0.1*2
        self.assertNotIn("ranch", seeded)  # no history match -> skipped
```

- [ ] **Step 2: Run, verify FAIL.**

- [ ] **Step 3: Implement**

```python
def _top_history_product(conn, item_key):
    return conn.execute(
        "SELECT product_name, COUNT(*) AS times, MAX(order_date) AS last_date, MAX(upc) AS upc "
        "FROM hyvee_purchase_history WHERE lower(product_name) LIKE ? "
        "GROUP BY product_name ORDER BY times DESC, last_date DESC LIMIT 1",
        (f"%{item_key.lower()}%",)).fetchone()

def cmd_seed(args) -> None:
    conn = _connect(); seeded = []
    for raw in args.items.split(","):
        key = normalize_item(raw)
        if not key:
            continue
        top = _top_history_product(conn, key)
        if not top:
            continue
        conf = min(0.9, 0.4 + 0.1 * top["times"])
        if conf < args.min_confidence:
            continue
        conn.execute(
            "INSERT INTO hyvee_item_prefs (item, preferred_upc, product_name, confidence, source, updated_at) "
            "VALUES (?,?,?,?, 'history', ?) "
            "ON CONFLICT(item) DO UPDATE SET preferred_upc=excluded.preferred_upc, "
            "product_name=excluded.product_name, confidence=excluded.confidence, "
            "source='history', updated_at=excluded.updated_at",
            (key, top["upc"], top["product_name"], conf, _now()))
        seeded.append(dict(conn.execute("SELECT * FROM hyvee_item_prefs WHERE item=?", (key,)).fetchone()))
    conn.commit()
    _out(seeded)
```

Wire the `seed` subparser (`--items` required, `--min-confidence` type=float default 0.0).

- [ ] **Step 4: Run, verify PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(hyvee): seed item prefs from purchase history"`

---

### Task 6: Resolve items + cart-run logging

**Files:** Modify `scripts/hyvee_store.py`; Test `tests/test_hyvee_store.py`

**Interfaces:**
- Produces: CLI `resolve --items-json <path>` where the file is a list of parsed items `[{"item": "...", "product_id": null, "upc": null, "brand": null, "size": null, "qty": 1, "note": null}]`. For each: if `product_id`/`upc` present → `{decision:"exact"}`; elif pref exists with `confidence>=AUTO_THRESHOLD` → `{decision:"auto", ...pref}`; elif pref exists → `{decision:"flag", ...pref}`; else `{decision:"search"}` (needs the browser layer to find candidates). Returns `{"resolved": [...]}`. Also `run log --items-json <p> --resolved-json <p> [--cart-verified 0|1] [--summary ...]` inserts a `hyvee_cart_runs` row and returns its id.

- [ ] **Step 1: Failing test**

```python
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
```

- [ ] **Step 2: Run, verify FAIL.**

- [ ] **Step 3: Implement**

```python
def cmd_resolve(args) -> None:
    items = json.loads(Path(args.items_json).read_text(encoding="utf-8"))
    conn = _connect(); resolved = []
    for it in items:
        key = normalize_item(it.get("item", ""))
        entry = {"item": key, "input": it}
        if it.get("product_id") or it.get("upc"):
            entry["decision"] = "exact"
            entry["product_id"] = it.get("product_id"); entry["upc"] = it.get("upc")
        else:
            pref = conn.execute("SELECT * FROM hyvee_item_prefs WHERE item=?", (key,)).fetchone()
            if pref and pref["confidence"] >= AUTO_THRESHOLD:
                entry["decision"] = "auto"; entry["pref"] = dict(pref)
            elif pref:
                entry["decision"] = "flag"; entry["pref"] = dict(pref)
            else:
                entry["decision"] = "search"
        resolved.append(entry)
    _out({"resolved": resolved})

def cmd_run_log(args) -> None:
    conn = _connect(); rid = uuid.uuid4().hex
    conn.execute("INSERT INTO hyvee_cart_runs (id, ts, items_json, resolved_json, cart_verified, summary) "
                 "VALUES (?,?,?,?,?,?)",
                 (rid, _now(), Path(args.items_json).read_text(encoding="utf-8"),
                  Path(args.resolved_json).read_text(encoding="utf-8"),
                  args.cart_verified, args.summary))
    conn.commit(); _out({"id": rid})
```

Wire `resolve` and `run log` subparsers.

- [ ] **Step 4: Run full suite, verify PASS** — `python -m unittest discover -s tests -v`
- [ ] **Step 5: Commit** — `git commit -am "feat(hyvee): item resolution decisions and cart-run logging"`

---

### Task 6.5: Rank search candidates (cost / sale / Hy-Vee-brand tie-breakers)

When an item has no confident history match and must be resolved by search, the candidate products need a deterministic, testable ranking. Tie-breaker order (best first), confirmed with the user: **(1) already in purchase history → (2) matches an explicit per-item `pref_brand` → (3) on sale → (4) lower cost (unit price, then price) → (5) Hy-Vee store brand as the safe cheap default.** A per-item `max_price` (if set) pushes over-budget candidates last. This lives in `hyvee_store.py` as a pure function so it is unit-tested, not encoded in subagent prose.

**Files:** Modify `scripts/hyvee_store.py`; Test `tests/test_hyvee_store.py`; also update the ranking sentence in `docs/superpowers/specs/2026-07-21-hyvee-cart-builder-decision-model-design.md` to match.

**Interfaces:**
- Produces: `rank_candidates(candidates, pref, history_names) -> list` (pure) and CLI `rank --item <k> --candidates-json <path>` which annotates each candidate with `in_history` (its name/upc appears in `hyvee_purchase_history`) and `is_hyvee_brand`, then returns candidates best-first each with a `_rank_reason`. Candidate dict shape (from `cart_ops.py search`): `{product_id, upc, name, brand, size, price, unit_price, on_sale}` where `price`/`unit_price` are strings like "$2.99" or null.
- Constants: `HYVEE_BRAND_MARKERS = ("hy-vee", "that's smart")`.

- [ ] **Step 1: Write failing test**

```python
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
```

- [ ] **Step 2: Run, verify FAIL.**

- [ ] **Step 3: Implement**

```python
HYVEE_BRAND_MARKERS = ("hy-vee", "that's smart")

def _money(s):
    if not s:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", str(s))
    return float(m.group(1)) if m else None

def _is_hyvee_brand(cand):
    blob = f"{cand.get('brand') or ''} {cand.get('name') or ''}".lower()
    return any(mark in blob for mark in HYVEE_BRAND_MARKERS)

def rank_candidates(candidates, pref, history_names):
    pref = pref or {}
    pref_brand = (pref.get("pref_brand") or "").lower().strip()
    max_price = pref.get("max_price")
    hist_lower = {h.lower() for h in history_names}
    def key(c):
        name = (c.get("name") or "").lower()
        in_history = any(h in name or name in h for h in hist_lower) or (
            c.get("upc") and c.get("upc") in history_names)
        brand_match = bool(pref_brand) and pref_brand in name
        price = _money(c.get("price"))
        unit = _money(c.get("unit_price"))
        over_budget = bool(max_price) and price is not None and price > max_price
        c["in_history"] = in_history; c["is_hyvee_brand"] = _is_hyvee_brand(c)
        # sort ascending: False(0)<True(1) so negate booleans we want first
        return (
            over_budget,                       # in-budget first
            not in_history,                    # history first
            not brand_match,                   # explicit brand pref
            not bool(c.get("on_sale")),        # on sale
            unit if unit is not None else float("inf"),   # lower unit cost
            price if price is not None else float("inf"), # lower price
            not c["is_hyvee_brand"],           # Hy-Vee brand default
        )
    ranked = sorted(candidates, key=key)
    for c in ranked:
        reasons = []
        if c.get("in_history"): reasons.append("bought before")
        if c.get("on_sale"): reasons.append("on sale")
        if c.get("is_hyvee_brand"): reasons.append("Hy-Vee brand")
        c["_rank_reason"] = ", ".join(reasons) or "cost"
    return ranked

def cmd_rank(args) -> None:
    cands = json.loads(Path(args.candidates_json).read_text(encoding="utf-8"))
    conn = _connect()
    key = normalize_item(args.item)
    pref = conn.execute("SELECT * FROM hyvee_item_prefs WHERE item=?", (key,)).fetchone()
    hist = conn.execute("SELECT product_name, upc FROM hyvee_purchase_history "
                        "WHERE lower(product_name) LIKE ?", (f"%{key}%",)).fetchall()
    names = [r["product_name"] for r in hist] + [r["upc"] for r in hist if r["upc"]]
    _out(rank_candidates(cands, dict(pref) if pref else None, names))
```

Wire the `rank` subparser (`--item` required, `--candidates-json` required).

- [ ] **Step 4: Run, verify PASS.**
- [ ] **Step 5: Update the spec** ranking sentence in the design doc to the confirmed order, then **commit** — `git commit -am "feat(hyvee): candidate ranking with sale/cost/Hy-Vee-brand tie-breakers"`

---

### Task 7: Shared web constants + reusable diagnostics toolkit

Goal: make the automation resilient to Hy-Vee site changes. Centralize every selector/URL/endpoint in one module, and turn the throwaway discovery probes into a maintained diagnostics CLI that pinpoints what broke.

**Files:**
- Create: `scripts/hyvee/hyvee_web.py`
- Create: `scripts/hyvee/diagnose.py`
- Delete: `scripts/hyvee/explore.py` (superseded)
- Reuse: `scripts/hyvee/login_test.py` helpers (`load_env`, `dismiss_cookie_banner`, session path)

**Interfaces:**
- `hyvee_web.py` produces module constants (verbatim from Phase-1 + discovery): URLs (`HOME_URL`, `LOGIN_URL`, `SEARCH_URL_TMPL`), API endpoints (`PURCHASE_HISTORY_API`, `GET_ACTIVE_CART_API`), and a `SELECTORS` dict (`cookie_accept`, `username`, `password`, `mfa`, `login_link`, `cart_icon`, `cart_bubble`, `search_input`, `add_to_cart`, `product_card`, `sponsored`, `brand`, `unit_of_measure`, `price_amount`, `is_buy_again`, `add_to_list`). Also a `CRITICAL_CHECKS` list describing each dependency (name, kind=url|selector|api, how to verify). `login_test.py` and `cart_ops.py` import from here rather than hardcoding.
- `diagnose.py` produces a CLI: `inspect --url <u>` (dump visible inputs/buttons/links + all `data-testid`s to JSON + full screenshot), `capture-api --url <u> [--match kw]` (record JSON network responses whose URL matches), `dump-card` (outerHTML + extracted fields of the first `product_card` on a milk search), and `check` (run every `CRITICAL_CHECKS` entry against the live site and print PASS/FAIL per dependency with a summary).

- [ ] **Step 1: Create `hyvee_web.py`** — move the selector/URL/endpoint constants out of `login_test.py` into this module; define `SELECTORS`, the URL/API constants, and `CRITICAL_CHECKS`. Example shape:

```python
# scripts/hyvee/hyvee_web.py
HOME_URL = "https://www.hy-vee.com/"
LOGIN_URL = "https://www.hy-vee.com/main/login"
SEARCH_URL_TMPL = "https://www.hy-vee.com/aisles-online/search?search={term}"
PURCHASE_HISTORY_API = "https://www.hy-vee.com/aisles-online/api/purchase-history/online?page={page}&pageSize=20"
GET_ACTIVE_CART_API = "https://www.hy-vee.com/aisles-online/api/graphql/three-legged/getActiveCart"

SELECTORS = {
    "cookie_accept": "#onetrust-accept-btn-handler",
    "username": "#username",
    "password": "#password",
    "mfa": "input[autocomplete='one-time-code'], input[name*='code' i], input[id*='code' i]",
    "login_link": "[data-testid='global-navigation-login']",
    "cart_icon": "[data-testid='global-navigation-cart-icon-button']",
    "cart_bubble": "[data-testid='global-navigation-cart-bubble']",
    "search_input": "[data-testid='global-navigation-search-input']",
    "add_to_cart": "[data-testid='add-to-cart-button']",
    "product_card": "[data-testid='UniversalProductCard']",
    "sponsored": "[data-testid='sponsored-text']",
    "unit_of_measure": "[class*='UnitOfMeasure']",
    "price_amount": "[data-testid='product-card-price-amount']",
    "is_buy_again": "[data-testid='isBuyAgain']",
}

# Each dependency the automation relies on, for diagnose.py `check`.
CRITICAL_CHECKS = [
    {"name": "login form", "kind": "selector", "url": LOGIN_URL, "selector": SELECTORS["username"]},
    {"name": "search input", "kind": "selector", "url": HOME_URL, "selector": SELECTORS["search_input"]},
    {"name": "product card", "kind": "selector", "url": SEARCH_URL_TMPL.format(term="milk"), "selector": SELECTORS["product_card"]},
    {"name": "add-to-cart button", "kind": "selector", "url": SEARCH_URL_TMPL.format(term="milk"), "selector": SELECTORS["add_to_cart"]},
    {"name": "cart bubble", "kind": "selector", "url": HOME_URL, "selector": SELECTORS["cart_bubble"]},
    {"name": "purchase-history API", "kind": "api", "url": PURCHASE_HISTORY_API.format(page=1), "expect_key": "purchaseGroups"},
    {"name": "getActiveCart API", "kind": "api", "url": GET_ACTIVE_CART_API, "method": "post"},
]
```

- [ ] **Step 2: Refactor `login_test.py`** to import `SELECTORS`/URLs from `hyvee_web.py` (no behavior change). Verify: `python scripts/hyvee/login_test.py --headless` still PASSes end-to-end (session reuse).

- [ ] **Step 3: Create `diagnose.py`** — port the discovery probes (element/testid inventory, `capture-api` network listener, `dump-card` product extraction) into subcommands, reusing the session + cookie helpers. Keep each subcommand self-contained and JSON/screenshot-emitting.

- [ ] **Step 4: Implement `diagnose.py check`** — iterate `CRITICAL_CHECKS`: for `selector`, load `url`, dismiss cookie, assert the selector is visible; for `api`, call via `page.request` and assert status 200 + `expect_key` present (or non-error for POST). Print a table of `PASS/FAIL` per dependency and exit non-zero if any fail. Verify: `python scripts/hyvee/diagnose.py check` prints all PASS against the live site today.

- [ ] **Step 5: Delete `explore.py`** — `git rm scripts/hyvee/explore.py` (capabilities now in `diagnose.py`).

- [ ] **Step 6: Document** — add a short "When the site breaks" section to a new `scripts/hyvee/README.md`: run `diagnose.py check` to find the failing dependency, then `diagnose.py inspect/capture-api/dump-card` to discover the new selector/endpoint, and update the one constant in `hyvee_web.py`.

- [ ] **Step 7: Commit** — `git add scripts/hyvee/hyvee_web.py scripts/hyvee/diagnose.py scripts/hyvee/README.md && git rm scripts/hyvee/explore.py && git commit -m "feat(hyvee): centralize web constants + reusable diagnostics toolkit"`

---

### Task 8: Browser layer — sync_history, search, add_to_cart, verify_cart

**Files:**
- Create: `scripts/hyvee/cart_ops.py`
- Import: all selectors/URLs/endpoints from `scripts/hyvee/hyvee_web.py` (Task 7) — never hardcode
- Reuse: `scripts/hyvee/login_test.py` helpers (`load_env`, `dismiss_cookie_banner`, session path)

**Interfaces:**
- Produces a CLI: `python scripts/hyvee/cart_ops.py <cmd>` printing JSON.
  - `sync-history [--max-pages N]` → fetches `/aisles-online/api/purchase-history/online?page=P&pageSize=20` via the logged-in `page.request`, concatenates `purchaseGroups`, writes a temp JSON and prints `{"json_path": "...", "orders": N}` for `hyvee_store.py history ingest`.
  - `search --term milk [--limit 12]` → search results, scroll to load, extract non-sponsored `UniversalProductCard` fields, printing a JSON list where each item uses the exact keys `hyvee_store.py rank` consumes: `{product_id (from /p/{id}/), upc (from image URL), name, brand, size, price, unit_price, on_sale, is_buy_again, badges}`. `price`/`unit_price` are the raw strings ("$2.99"); `on_sale` is true when a sale/was-now price or `badge-SALE`/`badge-DEAL` is present.
  - `add --product-id 4160380 [--qty 1]` → navigate to `/aisles-online/p/{id}`, click `[data-testid='add-to-cart-button']`, print `{"added": true, "cart_qty": N}` (bubble count).
  - `verify-cart` → POST getActiveCart via `page.request`, print `[{productId, upc, description, quantity}]`.

**This task is integration-tested against the live site (no unit test).** Reuse the Phase-1 patterns exactly (headed default, `--headless` flag, session reuse, cookie dismissal).

- [ ] **Step 1: Scaffold `cart_ops.py`** importing shared helpers from `login_test.py` (or copy the small helpers). Add argparse with the four subcommands and a shared `_browser(headless)` context manager mirroring `login_test.run`.

- [ ] **Step 2: Implement `sync-history`** using `page.request.get(...)` looping pages until `meta.pagination.pagesTotal`; cap with `--max-pages`. Verify: `python scripts/hyvee/cart_ops.py sync-history --max-pages 1` prints a json_path; then `python scripts/hyvee_store.py history ingest --json <that path>` returns `inserted > 0`.

- [ ] **Step 3: Implement `search`** — use the shared extraction (query `SELECTORS['product_card']`, skip `SELECTORS['sponsored']`, scroll ~4x to load); the field-extraction JS is shared with `diagnose.py dump-card`. Verify: `search --term milk` returns ≥5 non-sponsored items each with productId + price.

- [ ] **Step 4: Implement `add`** — navigate to product page, click add, read `[data-testid='global-navigation-cart-bubble']` before/after. Verify: `add --product-id <id from search>` increments the bubble. (Then remove the item manually / it's fine for the test.)

- [ ] **Step 5: Implement `verify-cart`** — replicate the captured `getActiveCart` request. Verify: after an `add`, `verify-cart` lists the product. Screenshot on error to `scripts/hyvee/last_error.png`.

- [ ] **Step 6: Commit** — `git add scripts/hyvee/cart_ops.py && git commit -m "feat(hyvee): browser layer for history sync, search, add, verify"`

---

### Task 9: The `hyvee` subagent

**Files:**
- Create: `.claude/agents/hyvee.md`

**Interfaces:** Consumes `scripts/hyvee_store.py` (all commands) + `scripts/hyvee/cart_ops.py`. Tools: `Bash` + the Todoist find/complete MCP tools (mirror `.claude/agents/todoist.md` tool list) + `mcp__claude_ai...` none else. Frontmatter `model: sonnet`.

- [ ] **Step 1: Write the agent definition** following the structure of `.claude/agents/lawn-garden.md` (bare-command mandate, JSON parsing, write to `agent_results` via `scripts/state_store.py`). Document the end-to-end flow:
  1. Read the Todoist shopping list (find the shopping project/list), parse each task's structured description (`item/brand/size/product_id/upc/qty/note`).
  2. `python scripts/hyvee_store.py resolve --items-json <tmp>`; for `search` decisions, call `cart_ops.py search --term <item>`, then `python scripts/hyvee_store.py rank --item <item> --candidates-json <tmp>` and take the top result as the proposed product (ranking = history → explicit brand pref → on-sale → lower cost → Hy-Vee brand default; the store owns this logic, the agent does not re-rank).
  3. Auto-add `auto`/`exact` decisions via `cart_ops.py add`. Collect `flag`/`search` items and ask the user ONE batched confirmation (present item → proposed product + price).
  4. Apply answers with `hyvee_store.py feedback record ...`; add confirmed products.
  5. `cart_ops.py verify-cart`; `hyvee_store.py run log ...`; post a review summary (never checkout).
  Include the one-time setup note: run `cart_ops.py sync-history` + `hyvee_store.py seed --items <staples>` before first use.

- [ ] **Step 2: Manual verification** — invoke the subagent against a small test shopping list (2–3 items incl. one new/ambiguous). Confirm confident items auto-add, the new item prompts once, cart verifies, prefs update. Confirm no checkout navigation occurred.

- [ ] **Step 3: Commit** — `git add .claude/agents/hyvee.md && git commit -m "feat(hyvee): add cart-builder subagent"`

---

### Task 10: Item-format adoption in meal-planner & todoist agents

**Files:**
- Modify: `.claude/agents/meal-planner.md`
- Modify: `.claude/agents/todoist.md`

- [ ] **Step 1: meal-planner** — where it adds the grocery list to Todoist, instruct it to write the structured description (`item:`, and `brand/size/qty/note` when confidently known) and to ask the user a brief clarifying question only for genuinely ambiguous staples before adding. Add an example task.

- [ ] **Step 2: todoist** — note the shopping-list item description convention so manual adds/updates preserve the `key: value` block; no behavior change otherwise.

- [ ] **Step 3: Manual verification** — ask meal-planner to add a small list; confirm the tasks carry the structured description and that `hyvee_store.py resolve` reads them cleanly.

- [ ] **Step 4: Commit** — `git commit -am "feat(hyvee): adopt structured shopping-list item format in meal-planner/todoist"`

---

## Self-Review Notes
- Spec coverage: item format (Task 10 + parsed in 6/9), 4 tables (1), history sync/ingest (2,8), prefs+normalization (3), confidence/learning (4), seed-from-history (5), resolution+auto/flag (6), tie-breaker ranking sale→cost→Hy-Vee-brand (6.5), centralized constants + diagnostics/maintainability (7), browser add/verify (8), clarification+orchestration (9), build-only guarantee (8 verify-cart / 9 flow), integrations (10). All covered.
- Types consistent: `normalize_item`, `AUTO_THRESHOLD`, `hyvee_item_prefs` columns, `SELECTORS` keys / `hyvee_web.py` constants, and `cart_ops.py` subcommand names are used identically across tasks.
- Maintainability: all site-coupled strings live only in `hyvee_web.py`; `diagnose.py check` verifies each and the discovery probes are preserved as subcommands for rediscovering changed selectors/endpoints.

## Verification (end-to-end)
1. `python -m unittest discover -s tests -v` → all store tests pass.
2. `python scripts/hyvee/diagnose.py check` → every critical selector/endpoint reports PASS against the live site (the maintenance safety net).
3. `python scripts/hyvee/cart_ops.py sync-history --max-pages 4` then `python scripts/hyvee_store.py history ingest --json <path>` → orders ingested.
4. `python scripts/hyvee_store.py seed --items "milk,eggs,bread,bananas,butter"` → prefs seeded from history.
5. Put 2–3 structured items on a test Todoist shopping list; invoke the `hyvee` subagent → confident items auto-add, ambiguous item prompts once, `verify-cart` lists them, prefs update via feedback.
6. Manually confirm the cart on hy-vee.com; **never place the order.**

## When the site changes (maintenance runbook)
`diagnose.py check` tells you *which* dependency broke. Then `diagnose.py inspect --url <page>` (element/testid inventory + screenshot), `capture-api --url <page>` (find the new JSON endpoint), or `dump-card` (product structure) reveal the new value. Fix the single constant in `hyvee_web.py` and re-run `check`. No other file should need touching for a pure selector/endpoint change.
```
