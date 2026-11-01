#!/usr/bin/env python3
"""Pull Monarch data into the local money snapshot (scripts/money.py).

Runs with the vendored Monarch server's interpreter, which has the monarch libraries and the
saved login (same as scripts/monarch_probe.py):

    vendor/monarch-mcp-server/.venv/bin/python scripts/money_sync.py [--mode auto|quick|window|full]

Modes:
  quick   current month's transactions, the needs-review list, budget and balances (Refresh button)
  window  quick + the last 120 days, net-worth history (year to date) and bills due in 14 days
  full    window + every transaction since the WHO start date (2025-06-01) and investment holdings
  auto    full when the last good full sync is over 20 hours old, otherwise window (the timer uses this)

Prints one JSON line: {"status": "ok"|"error"|"auth_failed", ...}. Exit 0 on ok, 1 otherwise.
An expired login is reported by the credential check's own probe, so this never alerts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import money  # noqa: E402

PAGE = 1000
WINDOW_DAYS = 120
RECURRING_DAYS = 14
FULL_EVERY = timedelta(hours=20)
AUTH_WORDS = ("401", "unauthor", "forbidden", "403", "login", "token", "session", "authentication needed")


# ---------------------------------------------------------------------------
# normalizing Monarch responses (pure functions, tested without the network)
# ---------------------------------------------------------------------------


def category_index(category_groups: list[dict]) -> dict:
    """category id -> (group name, group type, variability) from get_budgets' categoryGroups."""
    out = {}
    for g in category_groups or []:
        for c in g.get("categories") or []:
            out[str(c["id"])] = (g.get("name"), g.get("type"), c.get("budgetVariability") or g.get("budgetVariability"))
    return out


def normalize_transactions(results: list[dict], cats: dict) -> list[dict]:
    items = []
    for t in results or []:
        cat = t.get("category") or {}
        group = cats.get(str(cat.get("id")), (None, None, None))
        items.append({
            "id": str(t["id"]),
            "date": t["date"],
            "amount": t.get("amount") or 0.0,
            "merchant": (t.get("merchant") or {}).get("name") or t.get("plaidName"),
            "category_id": cat.get("id"),
            "category": cat.get("name"),
            "category_group": group[0],
            "group_type": group[1],
            "account_id": (t.get("account") or {}).get("id"),
            "account": (t.get("account") or {}).get("displayName"),
            "pending": t.get("pending"),
            "needs_review": t.get("needsReview"),
            "hide_from_reports": t.get("hideFromReports"),
            "tags": [x.get("name") for x in t.get("tags") or [] if x.get("name")],
            "updated_at": t.get("updatedAt"),
        })
    return items


def normalize_accounts(accounts: list[dict]) -> list[dict]:
    out = []
    for a in accounts or []:
        cred = a.get("credential") or {}
        out.append({
            "id": str(a["id"]),
            "name": a.get("displayName") or "",
            "type": (a.get("type") or {}).get("name"),
            "subtype": (a.get("subtype") or {}).get("name"),
            "institution": (cred.get("institution") or a.get("institution") or {}).get("name"),
            "data_provider": cred.get("dataProvider") or a.get("dataProvider") or ("MANUAL" if a.get("isManual") else None),
            "balance": a.get("displayBalance") if a.get("displayBalance") is not None else a.get("currentBalance"),
            "is_asset": a.get("isAsset", True),
            "include_in_net_worth": a.get("includeInNetWorth", True),
            "hidden": a.get("isHidden") or a.get("deactivatedAt") is not None,
            "update_required": cred.get("updateRequired"),
            "disconnected_at": cred.get("disconnectedFromDataProviderAt"),
            "last_updated_at": a.get("displayLastUpdatedAt"),
        })
    return out


_TOTAL_KINDS = {"totalIncome": "income", "totalExpenses": "expenses", "totalFixedExpenses": "fixed",
                "totalFlexibleExpenses": "flexible", "totalNonMonthlyExpenses": "non_monthly"}


def _amounts(block: dict | None) -> dict:
    block = block or {}
    planned = block.get("plannedAmount")
    if planned is None and "plannedCashFlowAmount" in block:
        planned = block.get("plannedCashFlowAmount")
    return {"planned": planned, "actual": block.get("actualAmount"), "remaining": block.get("remainingAmount"),
            "rollover": block.get("previousMonthRolloverAmount")}


def normalize_budget(data: dict, month: str) -> dict:
    """Totals plus one line per budgeted category (or per group, for groups budgeted as a whole)."""
    bd = data.get("budgetData") or {}
    totals = []
    for m in bd.get("totalsByMonth") or []:
        if m.get("month") == month:
            totals = [{"kind": kind, **_amounts(m.get(key))} for key, kind in _TOTAL_KINDS.items()]
    groups = {str(g["id"]): g for g in data.get("categoryGroups") or []}
    cat_group = {str(c["id"]): g for g in groups.values() for c in g.get("categories") or []}
    cat_info = {str(c["id"]): c for g in groups.values() for c in g.get("categories") or []}

    def month_amounts(entry: dict) -> dict | None:
        for ma in entry.get("monthlyAmounts") or []:
            if ma.get("month") == month:
                return _amounts(ma)
        return None

    lines = []
    for entry in bd.get("monthlyAmountsByCategoryGroup") or []:
        g = groups.get(str((entry.get("categoryGroup") or {}).get("id")))
        if not g or not g.get("groupLevelBudgetingEnabled"):
            continue
        amt = month_amounts(entry)
        if amt:
            lines.append({"line_id": str(g["id"]), "name": g["name"], "group_name": g["name"], "group_type": g.get("type"),
                          "level": "group", "variability": g.get("budgetVariability"), **amt})
    for entry in bd.get("monthlyAmountsByCategory") or []:
        cid = str((entry.get("category") or {}).get("id"))
        g = cat_group.get(cid)
        if not g or g.get("groupLevelBudgetingEnabled"):
            continue
        amt = month_amounts(entry)
        if amt:
            c = cat_info[cid]
            lines.append({"line_id": cid, "name": c["name"], "group_name": g["name"], "group_type": g.get("type"),
                          "level": "category", "variability": c.get("budgetVariability") or g.get("budgetVariability"),
                          **amt})
    return {"month": month, "totals": totals, "lines": lines}


def normalize_networth(data: dict) -> list[dict]:
    return [{"date": s["date"], "value": s["balance"]} for s in (data or {}).get("aggregateSnapshots") or []
            if s.get("balance") is not None]


def normalize_recurring(data: dict) -> list[dict]:
    out = []
    for r in (data or {}).get("recurringTransactionItems") or []:
        stream = r.get("stream") or {}
        out.append({
            "id": f"{stream.get('id')}:{r['date']}",
            "due_date": r["date"],
            "merchant": (stream.get("merchant") or {}).get("name"),
            "amount": r.get("amount") if r.get("amount") is not None else stream.get("amount"),
            "category": (r.get("category") or {}).get("name"),
            "account": (r.get("account") or {}).get("displayName"),
            "frequency": stream.get("frequency"),
            "is_past": r.get("isPast"),
            "transaction_id": r.get("transactionId"),
        })
    return out


def normalize_holdings(account_id: str, data: dict) -> list[dict]:
    """One row per aggregate holding from get_account_holdings."""
    edges = (((data or {}).get("portfolio") or {}).get("aggregateHoldings") or {}).get("edges") or []
    out = []
    for e in edges:
        node = e.get("node") or {}
        sec = node.get("security") or {}
        first = (node.get("holdings") or [{}])[0] or {}
        out.append({
            "account_id": str(account_id),
            "holding_id": str(node.get("id")),
            "ticker": sec.get("ticker") or first.get("ticker"),
            "name": sec.get("name") or first.get("name"),
            "security_type": sec.get("type") or first.get("type"),
            "quantity": node.get("quantity"),
            "value": node.get("totalValue"),
            "basis": node.get("basis"),
        })
    return out


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------


async def _all_transactions(client, start: str, end: str, **filters) -> list[dict]:
    results, offset = [], 0
    while True:
        page = await client.get_transactions(limit=PAGE, offset=offset, start_date=start, end_date=end, **filters)
        batch = (page.get("allTransactions") or {}).get("results") or []
        results.extend(batch)
        total = (page.get("allTransactions") or {}).get("totalCount") or 0
        offset += len(batch)
        if not batch or offset >= total:
            return results


async def _holdings(client, accounts: list[dict]) -> dict:
    """Holdings for every visible brokerage account with a balance. An account whose call fails is
    left out (its old rows stay) so one bad connection never fails the whole sync."""
    fetched, items = [], []
    for a in accounts:
        if a.get("type") != "brokerage" or a.get("hidden") or not a.get("balance"):
            continue
        try:
            data = await client.get_account_holdings(int(a["id"]))
        except Exception:  # noqa: BLE001
            continue
        fetched.append(a["id"])
        items.extend(normalize_holdings(a["id"], data))
    return {"accounts": fetched, "items": items}


async def fetch(mode: str, today: date, who_start: str) -> dict:
    from monarch_mcp_server.client import get_monarch_client

    client = await get_monarch_client()
    month = today.replace(day=1)
    month_end = (month + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    budgets = await client.get_budgets(start_date=month.isoformat(), end_date=month_end.isoformat())
    cats = category_index(budgets.get("categoryGroups"))

    if mode == "full":
        start = who_start
    elif mode == "window":
        start = (today - timedelta(days=WINDOW_DAYS)).isoformat()
    else:
        start = month.isoformat()
    end = today.isoformat()
    txns = await _all_transactions(client, start, end)
    review = await _all_transactions(client, who_start, end, needs_review=True)

    payload = {
        "transactions": {"start": start, "end": end,
                         # needs-review rows older than the range are upserted too (outside the replace range)
                         "items": normalize_transactions(txns, cats) + normalize_transactions(
                             [t for t in review if t["date"] < start], cats)},
        "needs_review": {"since": who_start, "ids": [str(t["id"]) for t in review]},
        "accounts": normalize_accounts((await client.get_accounts()).get("accounts")),
        "budget": normalize_budget(budgets, month.isoformat()),
    }
    if mode != "quick":
        nw = await client.get_aggregate_snapshots(start_date=date(today.year - 1, 1, 1).isoformat())
        payload["networth"] = normalize_networth(nw)
        r_start, r_end = today.isoformat(), (today + timedelta(days=RECURRING_DAYS)).isoformat()
        rec = await client.get_recurring_transactions(start_date=r_start, end_date=r_end)
        payload["recurring"] = {"start": r_start, "end": r_end, "items": normalize_recurring(rec)}
    if mode == "full":
        payload["holdings"] = await _holdings(client, payload["accounts"])
    return payload


def choose_mode(requested: str, db_path=None, now: datetime | None = None) -> str:
    if requested != "auto":
        return requested
    now = now or datetime.now()
    last = money.last_full_sync_at(db_path)
    return "full" if last is None or now - last > FULL_EVERY else "window"


def run(requested: str = "auto", db_path=None) -> dict:
    mode = choose_mode(requested, db_path)
    cfg = money.get_config(db_path)
    run_id = money.start_sync(mode, db_path)
    try:
        payload = asyncio.run(fetch(mode, date.today(), cfg["who_start_date"]))
        counts = money.apply_snapshot(payload, db_path)
    except Exception as exc:  # noqa: BLE001
        text = f"{type(exc).__name__}: {exc}"
        status = "auth_failed" if any(w in text.lower() for w in AUTH_WORDS) else "error"
        money.finish_sync(run_id, status, error=text, db_path=db_path)
        return {"status": status, "mode": mode, "error": text[:300]}
    money.finish_sync(run_id, "ok", counts=counts, db_path=db_path)
    return {"status": "ok", "mode": mode, **counts}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", default="auto", choices=("auto", *money.SYNC_MODES))
    ap.add_argument("--db", default=None, help="SQLite file (default: state/agent_results.db)")
    args = ap.parse_args(argv)
    logging.disable(logging.CRITICAL)  # the monarch libraries log session details
    result = run(args.mode, args.db)
    print(json.dumps(result))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
