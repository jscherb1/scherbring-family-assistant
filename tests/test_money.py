import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import money  # noqa: E402
import money_store  # noqa: E402
import money_sync  # noqa: E402


@pytest.fixture
def db(tmp_path):
    return tmp_path / "agent_results.db"


def txn(tid, d, amount, *, group="expense", tags=(), review=False, pending=False, merchant="Shop"):
    return {"id": tid, "date": d, "amount": amount, "merchant": merchant, "group_type": group, "tags": list(tags),
            "needs_review": review, "pending": pending}


def rows(db, sql, args=()):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


# ---- WHO tags ----

@pytest.mark.parametrize("name,expected", [
    ("WHO - Family", True), ("WHO - Justin", True), ("Kids", True), ("Adults", True), ("Family", True),
    ("who - family", False), ("Disney 2025", False), ("Whole Foods", False), (None, False), ("Family trip", False),
])
def test_who_tag_rule_matches_the_finance_agent(name, expected):
    assert money.is_who_tag(name) is expected


# ---- applying snapshots ----

def test_apply_snapshot_is_idempotent_and_replaces_its_range(db):
    snap = {"transactions": {"start": "2026-10-01", "end": "2026-10-31", "items": [
        txn("a", "2026-10-02", -10, tags=["WHO - Family", "Disney 2025"]), txn("b", "2026-10-03", -5)]}}
    money.apply_snapshot(snap, db_path=db)
    money.apply_snapshot(snap, db_path=db)
    got = {r["id"]: r for r in rows(db, "SELECT * FROM money_transactions")}
    assert set(got) == {"a", "b"} and got["a"]["has_who_tag"] == 1 and got["b"]["has_who_tag"] == 0
    # A row outside the range survives; one inside that Monarch no longer returns is removed.
    money.apply_snapshot({"transactions": {"start": "2026-09-01", "end": "2026-09-30",
                                           "items": [txn("old", "2026-09-05", -1)]}}, db_path=db)
    counts = money.apply_snapshot({"transactions": {"start": "2026-10-01", "end": "2026-10-31",
                                                    "items": [txn("a", "2026-10-02", -10)]}}, db_path=db)
    assert counts["transactions_removed"] == 1
    assert {r["id"] for r in rows(db, "SELECT id FROM money_transactions")} == {"a", "old"}


def test_needs_review_list_resets_everything_else_since_start(db):
    money.apply_snapshot({"transactions": {"start": "2025-01-01", "end": "2026-10-31", "items": [
        txn("before", "2025-05-01", -1, review=True), txn("x", "2026-10-01", -1, review=True),
        txn("y", "2026-10-02", -1, review=True)]}}, db_path=db)
    money.apply_snapshot({"needs_review": {"since": "2025-06-01", "ids": ["y"]}}, db_path=db)
    flags = {r["id"]: r["needs_review"] for r in rows(db, "SELECT id, needs_review FROM money_transactions")}
    assert flags == {"before": 1, "x": 0, "y": 1}


def test_review_queue_counts_posted_only(db):
    money.apply_snapshot({"transactions": {"start": "2025-01-01", "end": "2026-10-31", "items": [
        txn("r1", "2026-10-01", -1, review=True, tags=["WHO - Kids"]),
        txn("r2", "2026-10-02", -1, review=True, pending=True),
        txn("n1", "2025-07-01", -1), txn("n0", "2025-05-01", -1),
        txn("r3", "2026-10-04", -1, review=True),  # untagged but still waiting for review: only in that list
        txn("ok", "2026-10-03", -1, tags=["Family"])]}}, db_path=db)
    q = money.review_queue(db_path=db)
    assert q["needs_review"] == 2 and q["needs_review_pending"] == 1
    assert q["no_who_oldest"][0]["url"] == "https://app.monarch.com/transactions/n1"
    assert q["no_who"] == 1 and [t["id"] for t in q["no_who_oldest"]] == ["n1"]  # pending r2, pre-June n0 and unreviewed r3 excluded


def _log(db, lid, tid, action, proposed=None, decision=None):
    conn = money.connect(db)
    conn.execute("INSERT INTO finance_tag_log (id, ts, transaction_id, proposed_who_tag, action, user_decision) "
                 "VALUES (?, '2026-10-01T08:00:00', ?, ?, ?, ?)", (lid, tid, proposed, action, decision))
    conn.commit()
    conn.close()


def test_reconcile_settles_log_rows_the_user_handled_in_monarch(db):
    _log(db, "l1", "t1", "skipped_ambiguous")
    _log(db, "l2", "t1", "skipped_ambiguous")              # logged again by the next day's run
    _log(db, "l3", "t2", "skipped_ambiguous")              # still untagged
    _log(db, "l4", "t3", "tagged_auto", "WHO - Justin")    # user changed it
    _log(db, "l5", "t4", "tagged_auto", "WHO - Kids")      # kept and reviewed
    _log(db, "l6", "t5", "tagged_auto", "WHO - Kids")      # kept, still needs review: wait
    _log(db, "l7", "t9", "skipped_ambiguous")              # not in the snapshot: untouched
    _log(db, "l8", "t1", "tagged_confirmed", "WHO - Family", "confirmed")
    counts = money.apply_snapshot({"transactions": {"start": "2026-10-01", "end": "2026-10-31", "items": [
        txn("t1", "2026-10-01", -1, tags=["WHO - Family"]), txn("t2", "2026-10-01", -1),
        txn("t3", "2026-10-01", -1, tags=["WHO - Caroline"]), txn("t4", "2026-10-01", -1, tags=["WHO - Kids"]),
        txn("t5", "2026-10-01", -1, tags=["WHO - Kids"], review=True)]}}, db_path=db)
    assert counts["reconciled"] == {"resolved_in_monarch": 2, "corrected": 1, "confirmed": 1}
    d = {r["id"]: (r["user_decision"], r["note"]) for r in rows(db, "SELECT id, user_decision, note FROM finance_tag_log")}
    assert d["l1"][0] == d["l2"][0] == "resolved_in_monarch" and "WHO - Family" in d["l1"][1]
    assert d["l3"][0] is None and d["l6"][0] is None and d["l7"][0] is None
    assert d["l4"] == ("corrected", "changed in Monarch to WHO - Caroline") and d["l5"][0] == "confirmed"
    assert d["l8"] == ("confirmed", None)


# ---- normalizing Monarch responses ----

GROUPS = [
    {"id": "g1", "name": "Dining Out", "type": "expense", "budgetVariability": None, "groupLevelBudgetingEnabled": True,
     "categories": [{"id": "c1", "name": "Restaurants", "budgetVariability": "flexible"}]},
    {"id": "g2", "name": "Housing", "type": "expense", "budgetVariability": None, "groupLevelBudgetingEnabled": False,
     "categories": [{"id": "c2", "name": "Mortgage", "budgetVariability": "fixed"}]},
]


def test_normalize_transactions_and_categories():
    cats = money_sync.category_index(GROUPS)
    items = money_sync.normalize_transactions([{
        "id": 7, "date": "2026-10-20", "amount": -20.5, "pending": True, "needsReview": True, "hideFromReports": False,
        "plaidName": "SPOTIFY", "merchant": None, "category": {"id": "c1", "name": "Restaurants"},
        "account": {"id": "a1", "displayName": "Card"}, "tags": [{"name": "WHO - Family"}], "updatedAt": "x"}], cats)
    t = items[0]
    assert t["id"] == "7" and t["merchant"] == "SPOTIFY" and t["category_group"] == "Dining Out"
    assert t["group_type"] == "expense" and t["tags"] == ["WHO - Family"] and t["pending"] and t["needs_review"]


def test_normalize_budget_uses_group_lines_where_budgeted_as_a_whole():
    data = {"categoryGroups": GROUPS, "budgetData": {
        "totalsByMonth": [{"month": "2026-10-01",
                           "totalFlexibleExpenses": {"plannedAmount": 900, "actualAmount": 500, "remainingAmount": 400,
                                                     "previousMonthRolloverAmount": 0},
                           "totalNonMonthlyExpenses": {"plannedAmount": 300, "actualAmount": 68, "remainingAmount": -7404,
                                                       "previousMonthRolloverAmount": -7635}}],
        "monthlyAmountsByCategoryGroup": [{"categoryGroup": {"id": "g1"}, "monthlyAmounts": [
            {"month": "2026-10-01", "plannedCashFlowAmount": 900, "actualAmount": 1085, "remainingAmount": -185}]}],
        "monthlyAmountsByCategory": [
            {"category": {"id": "c1"}, "monthlyAmounts": [{"month": "2026-10-01", "plannedCashFlowAmount": 0, "actualAmount": 1085}]},
            {"category": {"id": "c2"}, "monthlyAmounts": [{"month": "2026-10-01", "plannedCashFlowAmount": 2078,
                                                            "actualAmount": 2078, "remainingAmount": 0}]}]}}
    b = money_sync.normalize_budget(data, "2026-10-01")
    kinds = {t["kind"]: t for t in b["totals"]}
    assert kinds["non_monthly"]["remaining"] == -7404 and kinds["flexible"]["planned"] == 900
    lines = {ln["name"]: ln for ln in b["lines"]}
    assert set(lines) == {"Dining Out", "Mortgage"}  # c1 is covered by its group's line
    assert lines["Dining Out"]["level"] == "group" and lines["Mortgage"]["variability"] == "fixed"


def test_normalize_accounts_and_recurring():
    acc = money_sync.normalize_accounts([{"id": 1, "displayName": "Card", "displayBalance": -50.0, "isAsset": False,
                                          "includeInNetWorth": True, "isHidden": False, "deactivatedAt": None,
                                          "type": {"name": "credit"}, "credential": {"dataProvider": "PLAID",
                                          "updateRequired": True, "institution": {"name": "Chase"}}}])
    assert acc[0]["data_provider"] == "PLAID" and acc[0]["update_required"] and acc[0]["institution"] == "Chase"
    rec = money_sync.normalize_recurring({"recurringTransactionItems": [{
        "stream": {"id": "s1", "frequency": "monthly", "amount": -5.0, "merchant": {"name": "DAKBOARD"}},
        "date": "2026-10-21", "isPast": False, "transactionId": None, "amount": -5.0}]})
    assert rec[0]["id"] == "s1:2026-10-21" and rec[0]["merchant"] == "DAKBOARD"



def test_holdings_normalize_and_replace_per_account(db):
    import asyncio
    rows = money_sync.normalize_holdings("a1", {"portfolio": {"aggregateHoldings": {"edges": [{"node": {
        "id": "h1", "quantity": 2.0, "basis": 0.0, "totalValue": 100.0,
        "holdings": [{"ticker": "FXAIX", "name": "FID 500", "type": "mutual_fund"}],
        "security": {"ticker": "FXAIX", "name": "Fidelity 500 Index Fund", "type": "mutual_fund"}}}]}}})
    assert rows == [{"account_id": "a1", "holding_id": "h1", "ticker": "FXAIX", "name": "Fidelity 500 Index Fund",
                     "security_type": "mutual_fund", "quantity": 2.0, "value": 100.0, "basis": 0.0}]
    money.apply_snapshot({"holdings": {"accounts": ["a1"], "items": rows}}, db_path=db)
    money.apply_snapshot({"holdings": {"accounts": ["a1"], "items": []}}, db_path=db)
    conn = money.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM money_holdings").fetchone()[0] == 0

    class Client:
        async def get_account_holdings(self, account_id):
            if account_id == 2:
                raise RuntimeError("provider error")
            return {"portfolio": {"aggregateHoldings": {"edges": []}}}

    accounts = [{"id": "1", "type": "brokerage", "balance": 10.0}, {"id": "2", "type": "brokerage", "balance": 5.0},
                {"id": "3", "type": "depository", "balance": 5.0}, {"id": "4", "type": "brokerage", "balance": 0.0}]
    out = asyncio.run(money_sync._holdings(Client(), accounts))
    assert out["accounts"] == ["1"]  # failed, non-brokerage and empty accounts are skipped

def test_auto_mode_runs_full_once_a_day(db):
    assert money_sync.choose_mode("auto", db) == "full"
    run = money.start_sync("full", db_path=db)
    money.finish_sync(run, "ok", db_path=db)
    assert money_sync.choose_mode("auto", db) == "window"
    assert money_sync.choose_mode("auto", db, now=datetime.now() + timedelta(hours=21)) == "full"
    assert money_sync.choose_mode("quick", db) == "quick"


def test_failed_fetch_is_recorded_as_auth_or_error(db, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("Authentication needed! Run: python login_setup.py")

    monkeypatch.setattr(money_sync, "fetch", boom)
    out = money_sync.run("quick", db)
    assert out["status"] == "auth_failed"
    st = money.sync_status(db_path=db)
    assert not st["has_data"] and st["last_run"]["status"] == "auth_failed"


# ---- overview metrics ----

def _budget(db, month, **kinds):
    money.apply_snapshot({"budget": {"month": month, "lines": [], "totals": [
        {"kind": k, "planned": p, "actual": a, "remaining": p - a, "rollover": 0} for k, (p, a) in kinds.items()]}},
        db_path=db)


@pytest.mark.parametrize("day,actual,status", [
    (1, 0, "green"), (1, 50, "yellow"),            # day 1 of 31: pace is ~32
    (15, 400, "green"), (15, 600, "yellow"),       # pace ~484 on the 15th
    (31, 1000, "green"), (31, 1001, "red"),        # last day: pace == the whole plan
])
def test_flexible_status_follows_the_month_pace(db, day, actual, status):
    _budget(db, "2026-10-01", flexible=(1000, actual))
    tile = next(b for b in money.overview(db, today=date(2026, 10, day))["budget"] if b["kind"] == "flexible")
    assert tile["status"] == status


@pytest.mark.parametrize("actual,status", [(100, "green"), (105, "yellow"), (106, "red")])
def test_fixed_status(db, actual, status):
    _budget(db, "2026-10-01", fixed=(100, actual))
    assert money.overview(db, today=date(2026, 10, 5))["budget"][0]["status"] == status


def test_non_monthly_ignores_the_rollover_hole(db):
    money.apply_snapshot({"budget": {"month": "2026-10-01", "lines": [], "totals": [
        {"kind": "non_monthly", "planned": 300, "actual": 68, "remaining": -7404, "rollover": -7635}]}}, db_path=db)
    tile = money.overview(db, today=date(2026, 10, 21))["budget"][0]
    assert tile["status"] == "green" and "-7,404" in tile["note"]


def test_spending_vs_last_month_and_normal(db):
    items = [txn("now", "2026-10-03", -110), txn("refund", "2026-10-04", 10),
             txn("sep", "2026-09-02", -100), txn("sep-late", "2026-09-20", -999),
             txn("aug", "2026-08-01", -80), txn("jul", "2026-07-05", -120),
             txn("xfer", "2026-10-02", -500, group="transfer"), txn("pay", "2026-10-01", 3000, group="income")]
    money.apply_snapshot({"transactions": {"start": "2026-07-01", "end": "2026-10-31", "items": items}}, db_path=db)
    s = money.overview(db, today=date(2026, 10, 5))["spending"]
    assert s["mtd"] == 100 and s["last_month_same_day"] == 100 and s["normal_same_day"] == 100
    assert s["status"] == "green"


def test_savings_runway_and_accounts(db):
    items = [txn(f"in{m}", f"2026-0{m}-01", 10000, group="income") for m in (7, 8, 9)]
    items += [txn(f"out{m}", f"2026-0{m}-02", -8000) for m in (7, 8, 9)]
    money.apply_snapshot({
        "transactions": {"start": "2026-07-01", "end": "2026-10-31", "items": items},
        "accounts": [
            {"id": "1", "name": "Checking", "type": "depository", "balance": 40000, "data_provider": "PLAID"},
            {"id": "2", "name": "Card", "type": "credit", "balance": -1500, "data_provider": "PLAID", "update_required": True},
            {"id": "3", "name": "Old", "type": "depository", "balance": 1, "data_provider": "MX",
             "last_updated_at": "2026-09-01T00:00:00+00:00"},
            {"id": "4", "name": "House", "type": "real_estate", "balance": 500000, "data_provider": "zillow",
             "last_updated_at": "2020-01-01"}]}, db_path=db)
    o = money.overview(db, today=date(2026, 10, 10))
    assert o["savings"]["rate_3mo"] == 20.0 and o["savings"]["status"] == "green"
    a = o["accounts"]
    assert a["runway_months"] == 5.0 and a["runway_status"] == "yellow" and a["card_balance"] == 1500
    assert a["needs_relink"] == ["Card"] and a["stale"] == ["Old"] and a["sync_status"] == "red"


def test_networth_bills_over_budget_and_retirement(db):
    money.apply_snapshot({
        "networth": [{"date": "2026-01-01", "value": 900}, {"date": "2026-09-21", "value": 1000},
                     {"date": "2026-10-21", "value": 1100}],
        "recurring": {"start": "2026-10-21", "end": "2026-11-04", "items": [
            {"id": "s:1", "due_date": "2026-10-23", "merchant": "Loan", "amount": -500},
            {"id": "s:2", "due_date": "2026-10-27", "merchant": "Paycheck", "amount": 2600},
            {"id": "s:3", "due_date": "2026-10-22", "merchant": "Paid", "amount": -5, "transaction_id": "t"}]},
        "budget": {"month": "2026-10-01", "totals": [], "lines": [
            {"line_id": "c1", "name": "Shopping", "group_type": "expense", "level": "category", "planned": 100, "actual": 150},
            {"line_id": "c2", "name": "Gas", "group_type": "expense", "level": "category", "planned": 100, "actual": 90},
            {"line_id": "c3", "name": "Pay", "group_type": "income", "level": "category", "planned": 100, "actual": 900}]}},
        db_path=db)
    conn = money.connect(db)
    conn.execute("INSERT INTO agent_results (agent, task, created_at, summary, detail_json) VALUES "
                 "('retirement', 't', '2026-07-27T00:00:00Z', 's', ?)", (json.dumps({"success_probability": 72.5}),))
    conn.commit()
    conn.close()
    o = money.overview(db, today=date(2026, 10, 21))
    assert o["networth"]["value"] == 1100 and o["networth"]["change_30d"] == 100 and o["networth"]["change_ytd"] == 200
    assert [b["merchant"] for b in o["bills"]["items"]] == ["Loan"] and o["bills"]["total"] == 500
    assert [x["name"] for x in o["over_budget"]] == ["Shopping"] and o["over_budget"][0]["over"] == 50
    assert o["retirement"] == {"success_pct": 72.5, "as_of": "2026-07-27", "status": "yellow"}


def test_projection(db):
    _budget(db, "2026-10-01", fixed=(1000, 400), flexible=(1000, 500), non_monthly=(100, 0))
    p = money.overview(db, today=date(2026, 10, 15))["projection"]
    assert p["projected"] == round(1000 + 500 / (15 / 31), 2) and p["planned"] == 2100 and p["status"] == "green"


def test_sync_status_staleness(db):
    assert money.sync_status(db_path=db)["stale"] is True
    run = money.start_sync("quick", db_path=db)
    money.finish_sync(run, "ok", db_path=db)
    assert money.sync_status(db_path=db)["stale"] is False
    later = datetime.now() + timedelta(hours=3)
    assert money.sync_status(db_path=db, now=later)["stale"] is True


# ---- config and CLI ----

def test_config_validation(db):
    assert money.set_config("spend_yellow_pct", "115", db_path=db)["spend_yellow_pct"] == "115"
    with pytest.raises(money.MoneyError):
        money.set_config("nope", "1", db_path=db)
    with pytest.raises(money.MoneyError):
        money.set_config("who_start_date", "June", db_path=db)
    with pytest.raises(money.MoneyError):
        money.set_config("queue_yellow", "lots", db_path=db)


def test_cli(db, monkeypatch, capsys):
    monkeypatch.setattr(money, "DB_PATH", db)
    assert money_store.main(["review"]) == 0
    assert json.loads(capsys.readouterr().out)["needs_review"] == 0
    assert money_store.main(["config", "set", "--key", "bogus", "--value", "1"]) == 2
    assert "unknown setting" in capsys.readouterr().err
    for cmd in ("overview", "budget", "spending", "sync-status", "config"):
        args = [cmd, "get"] if cmd == "config" else [cmd]
        assert money_store.main(args) == 0
        json.loads(capsys.readouterr().out)
