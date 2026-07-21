#!/usr/bin/env python3
"""Hy-Vee cart-builder store CLI for the hyvee subagent (stdlib only).

Owns purchase-history snapshot, learned item->product prefs, feedback log, and
cart-run log in the shared SQLite DB (state/agent_results.db, schema in
state/schema.sql). All commands print JSON to stdout. No browser logic here.
"""
from __future__ import annotations
import argparse, json, os, re, sqlite3, sys, uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "state" / "schema.sql"

AUTO_THRESHOLD = 0.75
STEP_UP = 0.1
STEP_DOWN = 0.2

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

def _clamp(x):
    return max(0.0, min(1.0, x))

def normalize_item(text: str) -> str:
    t = (text or "").lower().strip()
    t = re.sub(r"^\s*\d+\s*(x|ct|count|pk|pack|lb|lbs|oz|gal)?\s*", "", t)  # leading qty
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if t.endswith("s") and not t.endswith("ss"):
        t = t[:-1]
    return t

def _extract_upc(url: str):
    m = re.search(r"/products/(\d+)/", url or "")
    return m.group(1) if m else None

_PREF_FIELDS = ["preferred_product_id", "preferred_upc", "product_name", "size",
                "confidence", "pref_brand", "max_price", "prefer_on_sale", "pref_size", "source"]

def _pref_default(col, v):
    if v is not None:
        return v
    return {"confidence": 0.0, "prefer_on_sale": 0, "times_confirmed": 0,
            "times_rejected": 0, "source": "history"}.get(col, None)

def cmd_prefs_list(args) -> None:
    conn = _connect()
    rows = conn.execute("SELECT * FROM hyvee_item_prefs ORDER BY item").fetchall()
    _out([dict(r) for r in rows])

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

def cmd_prefs_get(args) -> None:
    conn = _connect()
    row = conn.execute("SELECT * FROM hyvee_item_prefs WHERE item=?", (normalize_item(args.item),)).fetchone()
    _out(dict(row) if row else {})

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

def cmd_resolve(args) -> None:
    items = json.loads(Path(args.items_json).read_text(encoding="utf-8"))
    conn = _connect(); resolved = []
    for it in items:
        key = normalize_item(it.get("item", ""))
        entry = {"item": it.get("item", ""), "input": it}
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

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="group", required=True)
    prefs = sub.add_parser("prefs").add_subparsers(dest="action", required=True)
    prefs.add_parser("list").set_defaults(func=cmd_prefs_list)
    ps = prefs.add_parser("set")
    ps.add_argument("--item", required=True)
    ps.add_argument("--preferred-product-id", default=None, dest="preferred_product_id")
    ps.add_argument("--preferred-upc", default=None, dest="preferred_upc")
    ps.add_argument("--product-name", default=None, dest="product_name")
    ps.add_argument("--size", default=None)
    ps.add_argument("--confidence", type=float, default=None)
    ps.add_argument("--pref-brand", default=None, dest="pref_brand")
    ps.add_argument("--max-price", type=float, default=None, dest="max_price")
    ps.add_argument("--prefer-on-sale", type=int, default=None, dest="prefer_on_sale")
    ps.add_argument("--pref-size", default=None, dest="pref_size")
    ps.add_argument("--source", default=None)
    ps.set_defaults(func=cmd_prefs_set)
    pg = prefs.add_parser("get")
    pg.add_argument("--item", required=True)
    pg.set_defaults(func=cmd_prefs_get)
    history = sub.add_parser("history").add_subparsers(dest="action", required=True)
    hi = history.add_parser("ingest"); hi.add_argument("--json", required=True)
    hi.set_defaults(func=cmd_history_ingest)
    hs = history.add_parser("stats"); hs.add_argument("--item", default=None)
    hs.set_defaults(func=cmd_history_stats)
    feedback = sub.add_parser("feedback").add_subparsers(dest="fb_command", required=True)
    fr = feedback.add_parser("record")
    fr.add_argument("--item", required=True)
    fr.add_argument("--action", required=True, choices=["accepted", "rejected", "substituted"])
    fr.add_argument("--proposed-product-id", default=None, dest="proposed_product_id")
    fr.add_argument("--chosen-product-id", default=None, dest="chosen_product_id")
    fr.add_argument("--note", default=None)
    fr.set_defaults(func=cmd_feedback_record)
    seed = sub.add_parser("seed")
    seed.add_argument("--items", required=True)
    seed.add_argument("--min-confidence", type=float, default=0.0, dest="min_confidence")
    seed.set_defaults(func=cmd_seed)
    resolve = sub.add_parser("resolve")
    resolve.add_argument("--items-json", required=True, dest="items_json")
    resolve.set_defaults(func=cmd_resolve)
    run_group = sub.add_parser("run").add_subparsers(dest="run_command", required=True)
    rl = run_group.add_parser("log")
    rl.add_argument("--items-json", required=True, dest="items_json")
    rl.add_argument("--resolved-json", required=True, dest="resolved_json")
    rl.add_argument("--cart-verified", type=int, default=None, dest="cart_verified")
    rl.add_argument("--summary", default=None)
    rl.set_defaults(func=cmd_run_log)
    rank = sub.add_parser("rank")
    rank.add_argument("--item", required=True)
    rank.add_argument("--candidates-json", required=True, dest="candidates_json")
    rank.set_defaults(func=cmd_rank)
    args = ap.parse_args(argv)
    args.func(args)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
