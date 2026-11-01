"""Money: the shared service over the local Monarch snapshot.

Zero-dependency (Python stdlib only), same SQLite file as state_store.py
(state/agent_results.db, schema in state/schema.sql).

Front ends, all thin wrappers over this module:
  - scripts/money_sync.py         pulls Monarch (vendored client) and calls apply_snapshot()
  - scripts/money_store.py        the CLI the `finance` subagent runs
  - dashboard/routers/finance.py  the Money pages and their /api twins

Monarch stays the source of truth. A sync replaces the snapshot rows it covers, and then
reconciles the finance agent's tagging log with what the user did in Monarch directly.

Every function takes an optional `db_path` (default: state/agent_results.db). Failures raise
MoneyError with a CLI-style code: 1 = not found, 2 = bad input.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "state" / "agent_results.db"
SCHEMA_PATH = REPO_ROOT / "state" / "schema.sql"

# A WHO tag, same rule as the finance agent (.claude/agents/finance.md): the tag name starts with
# "WHO" (case-sensitive, e.g. "WHO - Family"), or is one of the bare group tags.
WHO_GROUP_TAGS = {"Adults", "Kids", "Family"}

CONFIG_DEFAULTS = {
    "who_start_date": "2025-06-01",    # "transactions without a WHO tag" counts from here
    "stale_after_minutes": "120",      # snapshot older than this (inside sync hours) is flagged
    # Red / yellow / green thresholds for the overview tiles.
    "fixed_yellow_pct": "105",         # fixed bills: <=100% of plan green, <= this yellow, above red
    "non_monthly_yellow_pct": "110",
    "spend_yellow_pct": "110",         # spending vs normal (and total vs plan): <=100% green, <= this yellow
    "savings_green_pct": "20",         # savings rate: >= green, >= yellow, below red
    "savings_yellow_pct": "10",
    "runway_green_months": "6",        # emergency fund: months of spending covered by cash
    "runway_yellow_months": "3",
    "queue_yellow": "10",              # needs-review / no-WHO counts: 0 green, <= this yellow, above red
    "retirement_green_pct": "85",      # retirement model success probability
    "retirement_yellow_pct": "70",
    "account_stale_days": "3",         # a linked account with no new data this long is flagged
}

SYNC_MODES = ("quick", "window", "full")


class MoneyError(Exception):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.message = message
        self.code = code


# ---------------------------------------------------------------------------
# plumbing
# ---------------------------------------------------------------------------


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def is_who_tag(name: str | None) -> bool:
    return bool(name) and (name.startswith("WHO") or name in WHO_GROUP_TAGS)


def who_tags(tags: list[str]) -> list[str]:
    return [t for t in tags if is_who_tag(t)]


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def get_config(db_path=None) -> dict:
    conn = connect(db_path)
    try:
        stored = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM money_config")}
    finally:
        conn.close()
    return {**CONFIG_DEFAULTS, **stored}


def set_config(key: str, value: str, db_path=None) -> dict:
    if key not in CONFIG_DEFAULTS:
        raise MoneyError(f"unknown setting {key!r}; known: {', '.join(sorted(CONFIG_DEFAULTS))}")
    if key.endswith("_date"):
        try:
            date.fromisoformat(value)
        except ValueError:
            raise MoneyError(f"{key} must be YYYY-MM-DD") from None
    else:
        try:
            float(value)
        except ValueError:
            raise MoneyError(f"{key} must be a number") from None
    conn = connect(db_path)
    try:
        conn.execute("INSERT INTO money_config(key, value) VALUES(?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))
        conn.commit()
    finally:
        conn.close()
    return get_config(db_path)


# ---------------------------------------------------------------------------
# sync runs
# ---------------------------------------------------------------------------


def start_sync(mode: str, db_path=None) -> str:
    if mode not in SYNC_MODES:
        raise MoneyError(f"mode must be one of {', '.join(SYNC_MODES)}")
    run_id = uuid.uuid4().hex
    conn = connect(db_path)
    try:
        conn.execute("INSERT INTO money_sync_runs(id, started_at, mode, status) VALUES(?, ?, ?, 'running')",
                     (run_id, _now_iso(), mode))
        conn.commit()
    finally:
        conn.close()
    return run_id


def finish_sync(run_id: str, status: str, *, error: str | None = None, counts: dict | None = None,
                db_path=None) -> None:
    conn = connect(db_path)
    try:
        conn.execute("UPDATE money_sync_runs SET finished_at = ?, status = ?, error = ?, counts_json = ? WHERE id = ?",
                     (_now_iso(), status, (error or None) and error[:500], json.dumps(counts or {}), run_id))
        conn.commit()
    finally:
        conn.close()


def last_full_sync_at(db_path=None) -> datetime | None:
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT MAX(finished_at) AS t FROM money_sync_runs WHERE mode = 'full' AND status = 'ok'").fetchone()
    finally:
        conn.close()
    return datetime.fromisoformat(row["t"]) if row and row["t"] else None


def sync_status(db_path=None, now: datetime | None = None) -> dict:
    """When the snapshot was last refreshed, and whether that's recent enough to trust."""
    now = now or datetime.now()
    cfg = get_config(db_path)
    conn = connect(db_path)
    try:
        last = conn.execute("SELECT * FROM money_sync_runs ORDER BY started_at DESC LIMIT 1").fetchone()
        ok = conn.execute("SELECT finished_at, mode FROM money_sync_runs WHERE status = 'ok' "
                          "ORDER BY finished_at DESC LIMIT 1").fetchone()
        full = conn.execute("SELECT MAX(finished_at) AS t FROM money_sync_runs WHERE status = 'ok' AND mode = 'full'").fetchone()
    finally:
        conn.close()
    age_min = None
    if ok:
        age_min = round((now - datetime.fromisoformat(ok["finished_at"])).total_seconds() / 60)
    stale = age_min is None or age_min > float(cfg["stale_after_minutes"])
    return {
        "has_data": ok is not None,
        "last_ok_at": ok["finished_at"] if ok else None,
        "age_minutes": age_min,
        "stale": stale,
        "last_full_at": full["t"] if full else None,
        "last_run": None if last is None else {
            "started_at": last["started_at"], "finished_at": last["finished_at"], "mode": last["mode"],
            "status": last["status"], "error": last["error"],
        },
    }


# ---------------------------------------------------------------------------
# applying a snapshot
# ---------------------------------------------------------------------------


_TXN_COLS = ("id", "date", "amount", "merchant", "category_id", "category", "category_group", "group_type",
             "account_id", "account", "pending", "needs_review", "hide_from_reports", "tags_json",
             "has_who_tag", "updated_at", "synced_at")
_ACCOUNT_COLS = ("id", "name", "type", "subtype", "institution", "data_provider", "balance", "is_asset",
                 "include_in_net_worth", "hidden", "update_required", "disconnected_at", "last_updated_at",
                 "synced_at")


def _txn_row(t: dict, synced_at: str) -> tuple:
    tags = list(t.get("tags") or [])
    return (str(t["id"]), t["date"], float(t["amount"]), t.get("merchant"), t.get("category_id"),
            t.get("category"), t.get("category_group"), t.get("group_type"), t.get("account_id"),
            t.get("account"), int(bool(t.get("pending"))), int(bool(t.get("needs_review"))),
            int(bool(t.get("hide_from_reports"))), json.dumps(tags), int(bool(who_tags(tags))),
            t.get("updated_at"), synced_at)


def apply_snapshot(payload: dict, db_path=None) -> dict:
    """Write a normalized snapshot (built by money_sync.normalize_*) in one transaction.

    payload keys, each optional:
      transactions: {"start": d, "end": d, "items": [...]}  rows dated in [start, end] that aren't in
                    items are deleted (they were removed or re-dated in Monarch)
      needs_review: {"ids": [...], "since": d}  the complete needs-review list since `since`; every other
                    row since then gets needs_review = 0
      accounts:     [...]   replaces the account list
      budget:       {"month": "YYYY-MM-01", "totals": [...], "lines": [...]}  replaces that month
      networth:     [{"date", "value"}...]  upserted
      recurring:    {"start": d, "end": d, "items": [...]}  replaces that date range
      holdings:     {"accounts": [ids], "items": [...]}  replaces the holdings of each listed account
    Returns counts, plus what the tagging-log reconcile changed.
    """
    synced_at = _now_iso()
    counts: dict = {}
    conn = connect(db_path)
    try:
        with conn:
            txns = payload.get("transactions")
            if txns is not None:
                items = txns.get("items") or []
                conn.executemany(
                    f"INSERT OR REPLACE INTO money_transactions({', '.join(_TXN_COLS)}) "
                    f"VALUES({', '.join('?' * len(_TXN_COLS))})",
                    [_txn_row(t, synced_at) for t in items])
                ids = [str(t["id"]) for t in items]
                conn.execute("CREATE TEMP TABLE IF NOT EXISTS _seen (id TEXT PRIMARY KEY)")
                conn.execute("DELETE FROM _seen")
                conn.executemany("INSERT OR IGNORE INTO _seen(id) VALUES(?)", [(i,) for i in ids])
                gone = conn.execute("DELETE FROM money_transactions WHERE date BETWEEN ? AND ? "
                                    "AND id NOT IN (SELECT id FROM _seen)", (txns["start"], txns["end"])).rowcount
                counts["transactions"] = len(items)
                counts["transactions_removed"] = gone

            review = payload.get("needs_review")
            if review is not None:
                ids = [str(i) for i in review.get("ids") or []]
                conn.execute("CREATE TEMP TABLE IF NOT EXISTS _review (id TEXT PRIMARY KEY)")
                conn.execute("DELETE FROM _review")
                conn.executemany("INSERT OR IGNORE INTO _review(id) VALUES(?)", [(i,) for i in ids])
                conn.execute("UPDATE money_transactions SET needs_review = (id IN (SELECT id FROM _review)) "
                             "WHERE date >= ?", (review["since"],))
                counts["needs_review"] = len(ids)

            accounts = payload.get("accounts")
            if accounts is not None:
                conn.execute("DELETE FROM money_accounts")
                conn.executemany(
                    f"INSERT INTO money_accounts({', '.join(_ACCOUNT_COLS)}) VALUES({', '.join('?' * len(_ACCOUNT_COLS))})",
                    [(str(a["id"]), a.get("name") or "", a.get("type"), a.get("subtype"), a.get("institution"),
                      a.get("data_provider"), a.get("balance"), int(bool(a.get("is_asset", True))),
                      int(bool(a.get("include_in_net_worth", True))), int(bool(a.get("hidden"))),
                      int(bool(a.get("update_required"))), a.get("disconnected_at"), a.get("last_updated_at"),
                      synced_at) for a in accounts])
                counts["accounts"] = len(accounts)

            budget = payload.get("budget")
            if budget is not None:
                month = budget["month"]
                conn.execute("DELETE FROM money_budget_totals WHERE month = ?", (month,))
                conn.execute("DELETE FROM money_budget_lines WHERE month = ?", (month,))
                conn.executemany(
                    "INSERT INTO money_budget_totals(month, kind, planned, actual, remaining, rollover) "
                    "VALUES(?, ?, ?, ?, ?, ?)",
                    [(month, t["kind"], t.get("planned"), t.get("actual"), t.get("remaining"), t.get("rollover"))
                     for t in budget.get("totals") or []])
                conn.executemany(
                    "INSERT OR REPLACE INTO money_budget_lines(month, line_id, name, group_name, group_type, level, "
                    "variability, planned, actual, remaining, rollover) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(month, str(ln["line_id"]), ln["name"], ln.get("group_name"), ln.get("group_type"), ln["level"],
                      ln.get("variability"), ln.get("planned"), ln.get("actual"), ln.get("remaining"),
                      ln.get("rollover")) for ln in budget.get("lines") or []])
                counts["budget_lines"] = len(budget.get("lines") or [])

            networth = payload.get("networth")
            if networth is not None:
                conn.executemany("INSERT OR REPLACE INTO money_networth_history(date, value) VALUES(?, ?)",
                                 [(n["date"], float(n["value"])) for n in networth])
                counts["networth_days"] = len(networth)

            recurring = payload.get("recurring")
            if recurring is not None:
                conn.execute("DELETE FROM money_recurring WHERE due_date BETWEEN ? AND ?",
                             (recurring["start"], recurring["end"]))
                conn.executemany(
                    "INSERT OR REPLACE INTO money_recurring(id, due_date, merchant, amount, category, account, "
                    "frequency, is_past, transaction_id) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(r["id"], r["due_date"], r.get("merchant"), r.get("amount"), r.get("category"),
                      r.get("account"), r.get("frequency"), int(bool(r.get("is_past"))), r.get("transaction_id"))
                     for r in recurring.get("items") or []])
                counts["recurring"] = len(recurring.get("items") or [])

            holdings = payload.get("holdings")
            if holdings is not None:
                conn.executemany("DELETE FROM money_holdings WHERE account_id = ?",
                                 [(str(i),) for i in holdings.get("accounts") or []])
                conn.executemany(
                    "INSERT OR REPLACE INTO money_holdings(account_id, holding_id, ticker, name, security_type, "
                    "quantity, value, basis, synced_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(str(h["account_id"]), str(h["holding_id"]), h.get("ticker"), h.get("name"),
                      h.get("security_type"), h.get("quantity"), h.get("value"), h.get("basis"), synced_at)
                     for h in holdings.get("items") or []])
                counts["holdings"] = len(holdings.get("items") or [])

            counts["reconciled"] = _reconcile_tag_log(conn)
    finally:
        conn.close()
    return counts


def _reconcile_tag_log(conn: sqlite3.Connection) -> dict:
    """Close out finance-agent log rows that the user settled in Monarch directly.

    - skipped_ambiguous (the agent couldn't pick a WHO tag) -> 'resolved_in_monarch' once the
      transaction carries a WHO tag.
    - tagged_auto (the agent picked one) -> 'corrected' if the transaction's WHO tag is now a
      different one, 'confirmed' if it kept the agent's tag and was marked reviewed.
    Only rows whose transaction is in the snapshot are touched.
    """
    pending = conn.execute(
        "SELECT l.id, l.action, l.proposed_who_tag, t.tags_json, t.needs_review FROM finance_tag_log l "
        "JOIN money_transactions t ON t.id = l.transaction_id "
        "WHERE l.user_decision IS NULL AND l.action IN ('skipped_ambiguous', 'tagged_auto')"
    ).fetchall()
    changes = {"resolved_in_monarch": 0, "corrected": 0, "confirmed": 0}
    for r in pending:
        tags = who_tags(json.loads(r["tags_json"] or "[]"))
        if not tags:
            continue
        decision = note = None
        if r["action"] == "skipped_ambiguous":
            decision, note = "resolved_in_monarch", f"tagged in Monarch: {', '.join(tags)}"
        elif r["proposed_who_tag"] and r["proposed_who_tag"] not in tags:
            decision, note = "corrected", f"changed in Monarch to {', '.join(tags)}"
        elif not r["needs_review"]:
            decision, note = "confirmed", "kept and reviewed in Monarch"
        if decision:
            conn.execute("UPDATE finance_tag_log SET user_decision = ?, "
                         "note = COALESCE(note || ' | ', '') || ? WHERE id = ?", (decision, note, r["id"]))
            changes[decision] += 1
    return changes


# ---------------------------------------------------------------------------
# readers used by the tagging page and the agent
# ---------------------------------------------------------------------------


MONARCH_TXN_URL = "https://app.monarch.com/transactions/{}"


def _with_url(row) -> dict:
    d = dict(row)
    d["url"] = MONARCH_TXN_URL.format(d["id"])
    return d


def review_queue(db_path=None, *, limit: int = 8) -> dict:
    """Live counts from the snapshot: transactions waiting for review in Monarch (posted only), and
    reviewed transactions since the WHO start date without a WHO tag (so the two counts never overlap). Each comes with its oldest few."""
    cfg = get_config(db_path)
    since = cfg["who_start_date"]
    conn = connect(db_path)
    try:
        cols = "id, date, amount, merchant, category, account"
        needs = conn.execute(f"SELECT {cols} FROM money_transactions WHERE needs_review = 1 AND pending = 0 "
                             "ORDER BY date").fetchall()
        no_who = conn.execute(f"SELECT {cols} FROM money_transactions WHERE has_who_tag = 0 AND pending = 0 "
                              "AND needs_review = 0 AND date >= ? ORDER BY date", (since,)).fetchall()
        pending_review = conn.execute("SELECT COUNT(*) FROM money_transactions WHERE needs_review = 1 "
                                      "AND pending = 1").fetchone()[0]
    finally:
        conn.close()
    return {
        "needs_review": len(needs), "needs_review_pending": pending_review,
        "needs_review_oldest": [_with_url(r) for r in needs[:limit]],
        "no_who": len(no_who), "no_who_since": since, "no_who_oldest": [_with_url(r) for r in no_who[:limit]],
    }


# ---------------------------------------------------------------------------
# the overview (Money home page, Today tile, finance agent status answers)
# ---------------------------------------------------------------------------

GREEN, YELLOW, RED, NEUTRAL = "green", "yellow", "red", "neutral"
BUDGET_LABELS = {"fixed": "Fixed", "flexible": "Flexible", "non_monthly": "Non-monthly", "expenses": "Total spending",
                 "income": "Income"}


def _rank(value: float, green_max: float, yellow_max: float) -> str:
    """Lower is better: <= green_max green, <= yellow_max yellow, else red."""
    return GREEN if value <= green_max else YELLOW if value <= yellow_max else RED


def _rank_up(value: float, green_min: float, yellow_min: float) -> str:
    """Higher is better."""
    return GREEN if value >= green_min else YELLOW if value >= yellow_min else RED


def _pct(part: float | None, whole: float | None) -> float | None:
    if part is None or not whole:
        return None
    return round(100 * part / whole, 1)


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def _days_in_month(d: date) -> int:
    return (_add_months(_month_start(d), 1) - timedelta(days=1)).day


def _spend(conn, start: date, end: date) -> float:
    """Money out on expense categories (refunds net off), excluding hidden-from-reports rows."""
    row = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM money_transactions WHERE group_type = 'expense' "
                       "AND hide_from_reports = 0 AND date BETWEEN ? AND ?", (start.isoformat(), end.isoformat())).fetchone()
    return round(-row[0], 2)


def _income(conn, start: date, end: date) -> float:
    row = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM money_transactions WHERE group_type = 'income' "
                       "AND hide_from_reports = 0 AND date BETWEEN ? AND ?", (start.isoformat(), end.isoformat())).fetchone()
    return round(row[0], 2)


def _same_day_window(month: date, day: int) -> tuple[date, date]:
    return month, month.replace(day=min(day, _days_in_month(month)))


def _budget_tiles(conn, month: date, frac: float, cfg: dict) -> list[dict]:
    totals = {r["kind"]: dict(r) for r in conn.execute(
        "SELECT * FROM money_budget_totals WHERE month = ?", (month.isoformat(),))}
    tiles = []
    for kind in ("fixed", "flexible", "non_monthly", "expenses", "income"):
        t = totals.get(kind)
        if not t:
            continue
        planned, actual = t["planned"] or 0.0, t["actual"] or 0.0
        pct = _pct(actual, planned)
        tile = {"kind": kind, "label": BUDGET_LABELS[kind], "planned": planned, "actual": actual,
                "left": round(planned - actual, 2), "pct": pct, "rollover_balance": t["remaining"],
                "status": NEUTRAL, "note": ""}
        if kind == "flexible" and planned:
            pace = planned * frac
            tile["pace"] = round(pace, 2)
            tile["status"] = GREEN if actual <= pace else YELLOW if actual <= planned else RED
            tile["note"] = f"{'under' if actual <= pace else 'ahead of'} pace ({round(pace):,} by today)"
        elif kind == "fixed" and planned:
            tile["status"] = _rank(pct, 100, float(cfg["fixed_yellow_pct"]))
        elif kind == "non_monthly" and planned:
            # Monarch's remaining includes every month's rollover; this month's plan is the useful signal.
            tile["status"] = _rank(pct, 100, float(cfg["non_monthly_yellow_pct"]))
            tile["note"] = f"running balance with rollover: {round(t['remaining'] or 0):,}"
        elif kind == "income":
            tile["note"] = "received vs planned"
        tiles.append(tile)
    return tiles


def _projection(budget: list[dict], frac: float, cfg: dict) -> dict | None:
    by = {b["kind"]: b for b in budget}
    if "flexible" not in by or "fixed" not in by or not frac:
        return None
    fixed, flex = by["fixed"], by["flexible"]
    non_monthly = by.get("non_monthly", {"actual": 0.0, "planned": 0.0})
    projected = max(fixed["planned"], fixed["actual"]) + flex["actual"] / frac + non_monthly["actual"]
    planned = fixed["planned"] + flex["planned"] + non_monthly["planned"]
    pct = _pct(projected, planned)
    return {"projected": round(projected, 2), "planned": round(planned, 2), "pct": pct,
            "status": _rank(pct, 100, float(cfg["spend_yellow_pct"])) if pct is not None else NEUTRAL}


def _spending(conn, today: date, cfg: dict) -> dict:
    month = _month_start(today)
    mtd = _spend(conn, month, today)
    prev = []
    for back in (1, 2, 3):
        m = _add_months(month, -back)
        prev.append(_spend(conn, *_same_day_window(m, today.day)))
    normal = round(sum(prev) / len(prev), 2)
    pct = _pct(mtd, normal)
    return {"mtd": mtd, "last_month_same_day": prev[0], "normal_same_day": normal,
            "vs_last_pct": _pct(mtd, prev[0]), "vs_normal_pct": pct,
            "status": _rank(pct, 100, float(cfg["spend_yellow_pct"])) if pct is not None else NEUTRAL}


def _trailing_months(today: date, n: int = 3) -> tuple[date, date]:
    first = _add_months(_month_start(today), -n)
    return first, _month_start(today) - timedelta(days=1)


def _savings(conn, today: date, budget: list[dict], cfg: dict) -> dict:
    start, end = _trailing_months(today)
    income, spend = _income(conn, start, end), _spend(conn, start, end)
    rate = _pct(income - spend, income)
    by = {b["kind"]: b for b in budget}
    mtd_rate = None
    if by.get("income") and by.get("expenses") and by["income"]["actual"]:
        mtd_rate = _pct(by["income"]["actual"] - by["expenses"]["actual"], by["income"]["actual"])
    return {"rate_3mo": rate, "income_3mo": income, "spend_3mo": spend, "net_3mo": round(income - spend, 2),
            "rate_mtd": mtd_rate,
            "status": _rank_up(rate, float(cfg["savings_green_pct"]), float(cfg["savings_yellow_pct"]))
            if rate is not None else NEUTRAL}


def _accounts(conn, today: date, spend_3mo: float, cfg: dict) -> dict:
    rows = [dict(r) for r in conn.execute("SELECT * FROM money_accounts WHERE hidden = 0")]
    cash = sum(r["balance"] or 0 for r in rows if r["type"] == "depository" and r["include_in_net_worth"])
    cards = -sum(r["balance"] or 0 for r in rows if r["type"] == "credit" and (r["balance"] or 0) < 0)
    monthly = spend_3mo / 3 if spend_3mo else 0
    runway = round(cash / monthly, 1) if monthly else None
    stale_cut = (today - timedelta(days=int(float(cfg["account_stale_days"])))).isoformat()
    broken, stale = [], []
    for r in rows:
        if r["data_provider"] in (None, "MANUAL") or r["type"] in ("real_estate", "vehicle"):
            continue
        if r["update_required"] or r["disconnected_at"]:
            broken.append(r["name"])
        elif r["last_updated_at"] and r["last_updated_at"][:10] < stale_cut:
            stale.append(r["name"])
    return {
        "cash": round(cash, 2), "monthly_spend": round(monthly, 2), "runway_months": runway,
        "runway_status": _rank_up(runway, float(cfg["runway_green_months"]), float(cfg["runway_yellow_months"]))
        if runway is not None else NEUTRAL,
        "card_balance": round(cards, 2),
        "needs_relink": sorted(broken), "stale": sorted(stale),
        "sync_status": RED if broken else YELLOW if stale else GREEN,
    }


def _networth(conn, today: date) -> dict | None:
    rows = conn.execute("SELECT date, value FROM money_networth_history WHERE date >= ? ORDER BY date",
                        ((today - timedelta(days=400)).isoformat(),)).fetchall()
    if not rows:
        return None
    latest = rows[-1]

    def value_on_or_after(d: date) -> float | None:
        for r in rows:
            if r["date"] >= d.isoformat():
                return r["value"]
        return None

    d30 = value_on_or_after(today - timedelta(days=30))
    ytd = value_on_or_after(date(today.year, 1, 1))
    series = [{"date": r["date"], "value": r["value"]} for r in rows if r["date"] >= (today - timedelta(days=90)).isoformat()]
    return {"value": latest["value"], "as_of": latest["date"],
            "change_30d": None if d30 is None else round(latest["value"] - d30, 2),
            "change_ytd": None if ytd is None else round(latest["value"] - ytd, 2),
            "series": series}


def _bills(conn, today: date) -> dict:
    rows = [dict(r) for r in conn.execute(
        "SELECT due_date, merchant, amount, account FROM money_recurring WHERE due_date >= ? AND is_past = 0 "
        "AND transaction_id IS NULL AND amount < 0 ORDER BY due_date, merchant", (today.isoformat(),))]
    return {"items": rows, "total": round(-sum(r["amount"] for r in rows), 2)}


def _over_budget(conn, month: date, limit: int = 3) -> list[dict]:
    rows = conn.execute("SELECT name, group_name, planned, actual FROM money_budget_lines WHERE month = ? "
                        "AND group_type = 'expense' AND planned > 0 AND actual > planned "
                        "ORDER BY actual - planned DESC LIMIT ?", (month.isoformat(), limit)).fetchall()
    return [{**dict(r), "over": round(r["actual"] - r["planned"], 2)} for r in rows]


def _retirement(conn, cfg: dict) -> dict | None:
    row = conn.execute("SELECT created_at, detail_json FROM agent_results WHERE agent = 'retirement' "
                       "AND detail_json IS NOT NULL ORDER BY created_at DESC LIMIT 1").fetchone()
    if not row:
        return None
    try:
        p = float(json.loads(row["detail_json"]).get("success_probability"))
    except (TypeError, ValueError, AttributeError):
        return None
    return {"success_pct": p, "as_of": row["created_at"][:10],
            "status": _rank_up(p, float(cfg["retirement_green_pct"]), float(cfg["retirement_yellow_pct"]))}


def overview(db_path=None, today: date | None = None) -> dict:
    """Everything the Money home page shows, computed from the snapshot."""
    today = today or date.today()
    cfg = get_config(db_path)
    month = _month_start(today)
    frac = today.day / _days_in_month(today)
    queue = review_queue(db_path)
    yellow_q = float(cfg["queue_yellow"])
    conn = connect(db_path)
    try:
        budget = _budget_tiles(conn, month, frac, cfg)
        spending = _spending(conn, today, cfg)
        savings = _savings(conn, today, budget, cfg)
        accounts = _accounts(conn, today, savings["spend_3mo"], cfg)
        out = {
            "as_of": today.isoformat(), "month": month.isoformat(), "month_elapsed_pct": round(100 * frac),
            "sync": sync_status(db_path),
            "budget": budget,
            "projection": _projection(budget, frac, cfg),
            "spending": spending,
            "savings": savings,
            "accounts": accounts,
            "networth": _networth(conn, today),
            "bills": _bills(conn, today),
            "over_budget": _over_budget(conn, month),
            "retirement": _retirement(conn, cfg),
            "review": {**queue,
                       "needs_review_status": _rank(queue["needs_review"], 0, yellow_q),
                       "no_who_status": _rank(queue["no_who"], 0, yellow_q)},
        }
    finally:
        conn.close()
    return out
