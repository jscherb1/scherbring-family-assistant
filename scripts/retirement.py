"""Retirement planning: the single shared service.

Both front ends are thin wrappers over this module, so the inputs are derived one way only:
  - scripts/retirement_store.py   the CLI the `retirement` and `finance-advisor` subagents run
  - (Phase 3) the dashboard section and its /api twin

Inputs come from the local Monarch snapshot (the money_* tables that scripts/money_sync.py
refreshes; holdings on full syncs) plus config in finance_config:
  retirement_assumptions   the user's choices (ages, spend, inflation, SS, glide path, scenarios)
  retirement_accounts      per-account overrides: {account_id: {bucket, allocation, note}}
  retirement_securities    per-ticker overrides: {TICKER: {us_equity, intl_equity, bonds, cash}}

The Monte Carlo itself lives in scripts/retirement_model.py (numpy); this module resolves the
assumptions, runs it, and writes the run files under state/retirement/.

Every function takes an optional `db_path` (default: state/agent_results.db) and, where dates
matter, an optional `today`. Failures raise RetirementError with a CLI-style code:
1 = not found, 2 = bad input.

Account buckets
  pretax | roth | hsa | taxable   count toward the retirement balance
  cash                            checking/savings; reported, not invested in the projection
  kids                            529s, UTMAs and other money earmarked for the kids; excluded
  exclude                         home, cars, loans, cards, hidden or closed accounts
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

import money
import retirement_model as rm

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "state" / "agent_results.db"
SCHEMA_PATH = REPO_ROOT / "state" / "schema.sql"
RUN_DIR = REPO_ROOT / "state" / "retirement"

ASSUMPTIONS_KEY = "retirement_assumptions"
ACCOUNTS_KEY = "retirement_accounts"
SECURITIES_KEY = "retirement_securities"

BUCKETS = ("pretax", "roth", "hsa", "taxable", "cash", "kids", "exclude")
RETIREMENT_BUCKETS = ("pretax", "roth", "hsa", "taxable")
CLASSES = rm.ASSET_CLASSES

# Monarch account subtypes -> bucket. Anything brokerage-typed but unlisted is assumed taxable
# (and flagged); depository accounts are cash; every other type is excluded.
SUBTYPE_BUCKETS = {
    **dict.fromkeys(("ira", "sep_ira", "simple_ira", "rollover_ira", "traditional_ira", "st_401k", "st_403b",
                     "st_401a", "st_457b", "401k", "403b", "401a", "457b", "keogh", "thrift_savings_plan",
                     "pension"), "pretax"),
    **dict.fromkeys(("roth", "roth_ira", "roth_401k", "st_roth_401k", "roth_403b", "st_roth_403b"), "roth"),
    **dict.fromkeys(("hsa", "health_savings_account"), "hsa"),
    **dict.fromkeys(("education_savings_account", "529", "utma", "ugma", "coverdell", "coverdell_esa"), "kids"),
    **dict.fromkeys(("brokerage", "cryptocurrency", "individual", "joint", "trust", "non_taxable_brokerage_account"),
                    "taxable"),
}

US, INTL, BONDS, CASH = ({c: 1.0 if c == k else 0.0 for c in CLASSES} for k in CLASSES)

# Funds the household holds today, classified by hand. Anything else falls back to the
# name heuristics in classify_security (and is flagged).
KNOWN_SECURITIES = {
    **dict.fromkeys(("VIIIX", "FXAIX", "VITPX", "FSMAX", "IVW", "FBGRX", "FBCG", "FSELX", "QUASX", "ACN",
                     "VTSAX", "VTI", "VOO", "SPY", "ITOT", "SCHB", "FSKAX", "FZROX"), US),
    **dict.fromkeys(("FSPSX", "FPADX", "CEMIX", "VTIAX", "VXUS", "IXUS", "FTIHX", "FZILX"), INTL),
    **dict.fromkeys(("FNBGX", "FXNAX", "BND", "AGG", "VBTLX", "FUAMX"), BONDS),
    **dict.fromkeys(("SPAXX", "FDRXX", "FZFXX", "CORE"), CASH),
}

# Target-date funds held today -> target year (mix from _target_date_mix).
KNOWN_TARGET_DATE = {"FDEEX": 2055, "FDKLX": 2060, "FFFHX": 2050}

DEFAULT_GLIDE = {"retirement_equity": 0.60, "end_equity": 0.40}

_CASH_WORDS = ("money market", "deposit", "sweep", "cash reserve")
_BOND_WORDS = ("bond", "treasury", "fixed income", "aggregate", "tips", "municipal", "income fund")
_INTL_WORDS = ("intl", "international", "emerging", "emkts", "emrg", "ex-us", "ex us", "developed",
               "foreign", "europe", "pacific", "overseas", "world ex")
_TARGET_DATE = re.compile(r"\b(20[2-8]\d)\b")
_TARGET_WORDS = ("freedom", "target", "lifecycle", "retirement", "portfolio", "lifepath")


class RetirementError(Exception):
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


def _today(today: date | None) -> date:
    return today or datetime.now().date()


def _get_json(key: str, default, db_path=None):
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT value FROM finance_config WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except ValueError:
        raise RetirementError(f"finance_config {key} is not valid JSON") from None


def _set_json(key: str, value, db_path=None) -> None:
    conn = connect(db_path)
    try:
        conn.execute("INSERT INTO finance_config(key, value) VALUES(?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))
        conn.commit()
    finally:
        conn.close()


def _norm_alloc(alloc: dict, what: str) -> dict:
    """Validate and normalize an allocation dict to weights over CLASSES summing to 1."""
    if not isinstance(alloc, dict) or not alloc:
        raise RetirementError(f"{what}: allocation must be an object like {{\"us_equity\": 0.6, \"bonds\": 0.4}}")
    unknown = set(alloc) - set(CLASSES)
    if unknown:
        raise RetirementError(f"{what}: unknown asset classes {sorted(unknown)}; known: {', '.join(CLASSES)}")
    try:
        w = {c: float(alloc.get(c, 0.0)) for c in CLASSES}
    except (TypeError, ValueError):
        raise RetirementError(f"{what}: allocation weights must be numbers") from None
    total = sum(w.values())
    if any(v < 0 for v in w.values()) or total <= 0:
        raise RetirementError(f"{what}: allocation weights must be non-negative with a positive total")
    if total > 1.5:  # given as percentages
        w = {c: v / 100.0 for c, v in w.items()}
        total = sum(w.values())
    if abs(total - 1.0) > 0.02:
        raise RetirementError(f"{what}: allocation weights must sum to 1 (got {total:.3f})")
    return {c: v / total for c, v in w.items()}


def _equity_split(equity: float, intl_share: float = 0.3) -> dict:
    return {"us_equity": equity * (1 - intl_share), "intl_equity": equity * intl_share,
            "bonds": 1 - equity, "cash": 0.0}


# ---------------------------------------------------------------------------
# assumptions config
# ---------------------------------------------------------------------------


def get_assumptions(db_path=None) -> dict:
    a = _get_json(ASSUMPTIONS_KEY, None, db_path)
    if not isinstance(a, dict):
        raise RetirementError("no retirement_assumptions in finance_config", code=1)
    return a


def set_assumption(key: str, value, db_path=None) -> dict:
    """Set one field of retirement_assumptions (value already JSON-decoded). Validates the result."""
    a = get_assumptions(db_path)
    if value is None:
        a.pop(key, None)
    else:
        a[key] = value
    if key in ("allocation_override",) and value is not None:
        a[key] = _norm_alloc(value, key)
    trial = {**a, **{k: 1 for k in ("current_balance", "annual_contribution") if a.get(k) is None}}
    if not trial.get("allocation"):
        trial["allocation"] = a.get("allocation_override") or _equity_split(float(a.get("fallback_stock_pct", 80)) / 100)
    trial["glide_path"] = a.get("glide_path", DEFAULT_GLIDE)
    trial.pop("expected_return", None)
    trial.pop("volatility", None)
    problems = rm.validate(trial)
    if problems:
        raise RetirementError("; ".join(problems))
    _set_json(ASSUMPTIONS_KEY, a, db_path)
    return a


# ---------------------------------------------------------------------------
# accounts and securities
# ---------------------------------------------------------------------------


def account_overrides(db_path=None) -> dict:
    return _get_json(ACCOUNTS_KEY, {}, db_path)


def security_overrides(db_path=None) -> dict:
    return _get_json(SECURITIES_KEY, {}, db_path)


def _accounts_rows(db_path=None) -> list[dict]:
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM money_accounts ORDER BY type, name")]
    finally:
        conn.close()


def resolve_account(ref: str, db_path=None) -> dict:
    """Find an account by id, exact name, or unique name fragment (case-insensitive)."""
    rows = _accounts_rows(db_path)
    ref_l = (ref or "").strip().lower()
    if not ref_l:
        raise RetirementError("account is required")
    for r in rows:
        if r["id"] == ref or r["name"].lower() == ref_l:
            return r
    hits = [r for r in rows if ref_l in r["name"].lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise RetirementError(f"no account matches {ref!r}", code=1)
    raise RetirementError(f"{ref!r} matches several accounts: " + "; ".join(h["name"] for h in hits[:8]))


def set_account(ref: str, bucket: str | None = None, allocation: dict | None = None,
                note: str | None = None, clear: bool = False, db_path=None) -> dict:
    acct = resolve_account(ref, db_path)
    if bucket is not None and bucket not in BUCKETS:
        raise RetirementError(f"bucket must be one of {', '.join(BUCKETS)}")
    overrides = account_overrides(db_path)
    if clear:
        overrides.pop(acct["id"], None)
    else:
        entry = dict(overrides.get(acct["id"], {}))
        entry["name"] = acct["name"]
        if bucket is not None:
            entry["bucket"] = bucket
        if allocation is not None:
            entry["allocation"] = _norm_alloc(allocation, acct["name"])
        if note is not None:
            entry["note"] = note
        overrides[acct["id"]] = entry
    _set_json(ACCOUNTS_KEY, overrides, db_path)
    return next(a for a in classify_accounts(db_path) if a["id"] == acct["id"])


def set_security(ticker: str, allocation: dict | None, db_path=None) -> dict:
    t = (ticker or "").strip().upper()
    if not t:
        raise RetirementError("ticker is required")
    overrides = security_overrides(db_path)
    if allocation is None:
        overrides.pop(t, None)
    else:
        overrides[t] = _norm_alloc(allocation, t)
    _set_json(SECURITIES_KEY, overrides, db_path)
    return {"ticker": t, "allocation": overrides.get(t), "overrides": len(overrides)}


def _target_date_mix(target_year: int, today: date) -> dict:
    """Approximate a target-date fund's mix from its year: ~90% stocks until 25 years out,
    sliding to ~50% at the target year and ~30% seven years after; 40% of stocks international."""
    yt = target_year - today.year
    if yt >= 25:
        eq = 0.90
    elif yt >= 0:
        eq = 0.50 + 0.40 * yt / 25
    else:
        eq = max(0.30, 0.50 + 0.20 * yt / 7)
    return _equity_split(eq, intl_share=0.40)


def classify_security(ticker: str | None, name: str | None, security_type: str | None,
                      overrides: dict | None = None, today: date | None = None) -> tuple[dict, str]:
    """(allocation weights, source) for one holding. Source is 'override', 'known', or
    'heuristic: ...' (heuristic results are worth confirming)."""
    t = (ticker or "").upper()
    if overrides and t in overrides:
        return overrides[t], "override"
    if t in KNOWN_SECURITIES:
        return KNOWN_SECURITIES[t], "known"
    if t in KNOWN_TARGET_DATE:
        return _target_date_mix(KNOWN_TARGET_DATE[t], _today(today)), f"known target-date {KNOWN_TARGET_DATE[t]}"
    n = (name or "").lower()
    st = (security_type or "").lower()
    if st == "cryptocurrency":
        return US, "crypto (priced as US stock)"
    if st == "cash" or any(w in n for w in _CASH_WORDS):
        return CASH, "heuristic: cash"
    m = _TARGET_DATE.search(n)
    if m and any(w in n for w in _TARGET_WORDS):
        return _target_date_mix(int(m.group(1)), _today(today)), f"heuristic: target-date {m.group(1)}"
    if any(w in n for w in _BOND_WORDS):
        return BONDS, "heuristic: bond fund"
    if any(w in n for w in _INTL_WORDS):
        return INTL, "heuristic: international stock"
    return US, "heuristic: assumed US stock"


def _holdings_by_account(db_path=None) -> dict[str, list[dict]]:
    conn = connect(db_path)
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM money_holdings ORDER BY value DESC")]
    finally:
        conn.close()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["account_id"], []).append(r)
    return out


def classify_accounts(db_path=None) -> list[dict]:
    """Every account in the snapshot with its bucket and where that came from."""
    overrides = account_overrides(db_path)
    out = []
    for a in _accounts_rows(db_path):
        ov = overrides.get(a["id"], {})
        if ov.get("bucket"):
            bucket, source = ov["bucket"], "override"
        elif a["hidden"]:
            bucket, source = "exclude", "hidden in Monarch"
        elif a["type"] == "depository":
            bucket, source = "cash", "type"
        elif a["subtype"] in SUBTYPE_BUCKETS:
            bucket, source = SUBTYPE_BUCKETS[a["subtype"]], f"subtype {a['subtype']}"
        elif a["type"] == "brokerage":
            bucket, source = "taxable", f"assumed (unrecognized subtype {a['subtype']!r})"
        else:
            bucket, source = "exclude", "type"
        out.append({"id": a["id"], "name": a["name"], "type": a["type"], "subtype": a["subtype"],
                    "institution": a["institution"], "balance": round(float(a["balance"] or 0.0), 2),
                    "bucket": bucket, "source": source, "note": ov.get("note"),
                    "allocation_override": ov.get("allocation")})
    return out


def account_allocation(acct: dict, holdings: list[dict], sec_overrides: dict, fallback: dict,
                       today: date | None = None) -> tuple[dict, str, list[str]]:
    """(weights, source, flagged holdings) for one account."""
    if acct.get("allocation_override"):
        return acct["allocation_override"], "account override", []
    if acct["bucket"] == "cash":
        return CASH, "cash account", []
    total = sum(float(h["value"] or 0.0) for h in holdings)
    if total <= 0:
        return fallback, "fallback (no holdings from Monarch)", []
    w = {c: 0.0 for c in CLASSES}
    flagged = []
    for h in holdings:
        alloc, src = classify_security(h["ticker"], h["name"], h["security_type"], sec_overrides, today)
        share = float(h["value"] or 0.0) / total
        for c in CLASSES:
            w[c] += share * float(alloc.get(c, 0.0))
        if src.startswith("heuristic") and src != "heuristic: cash":
            flagged.append(f"{h['ticker'] or h['name']} ({src[11:]})")
    return w, "holdings", flagged


# ---------------------------------------------------------------------------
# deriving inputs
# ---------------------------------------------------------------------------


def trailing_savings(db_path=None, today: date | None = None, months: int = 12) -> dict:
    """Income minus spending over the last `months` full months, from the snapshot."""
    today = _today(today)
    start, end = money._trailing_months(today, months)
    conn = money.connect(db_path)
    try:
        income, spend = money._income(conn, start, end), money._spend(conn, start, end)
    finally:
        conn.close()
    return {"start": start.isoformat(), "end": end.isoformat(), "income": income, "spend": spend,
            "savings": round(income - spend, 2)}


def derive_inputs(db_path=None, today: date | None = None) -> dict:
    """Balances by bucket, the retirement portfolio's allocation, and trailing savings, all
    from the snapshot, with a source string per value and warnings for anything assumed."""
    today = _today(today)
    stored = get_assumptions(db_path)
    status = money.sync_status(db_path)
    warnings: list[str] = []
    if not status["has_data"]:
        raise RetirementError("the Monarch snapshot is empty; run money_sync.py first", code=1)
    if status["stale"]:
        warnings.append(f"Monarch snapshot is stale (last good sync {status['last_ok_at']})")

    fallback = stored.get("allocation_override") or _equity_split(float(stored.get("fallback_stock_pct", 80)) / 100)
    sec_ov = security_overrides(db_path)
    holdings = _holdings_by_account(db_path)
    accounts = classify_accounts(db_path)
    buckets = {b: 0.0 for b in BUCKETS}
    weighted = {c: 0.0 for c in CLASSES}
    detail = []
    for a in accounts:
        buckets[a["bucket"]] += a["balance"]
        if a["source"].startswith("assumed"):
            warnings.append(f"{a['name']}: {a['source']}, counted as taxable; confirm with accounts classify")
        if a["bucket"] not in RETIREMENT_BUCKETS or a["balance"] <= 0:
            continue
        w, src, flagged = account_allocation(a, holdings.get(a["id"], []), sec_ov, fallback, today)
        if src.startswith("fallback"):
            warnings.append(f"{a['name']} (${a['balance']:,.0f}): no holdings from Monarch; assumed "
                            f"{(w['us_equity'] + w['intl_equity']) * 100:.0f}% stocks. Set its mix with "
                            f"accounts classify --allocation")
        if flagged:
            warnings.append(f"{a['name']}: classified by name, worth confirming: {', '.join(flagged)}")
        for c in CLASSES:
            weighted[c] += a["balance"] * w[c]
        detail.append({"id": a["id"], "name": a["name"], "bucket": a["bucket"], "balance": a["balance"],
                       "allocation": {c: round(v, 4) for c, v in w.items()}, "allocation_source": src})

    retirement_balance = round(sum(buckets[b] for b in RETIREMENT_BUCKETS), 2)
    allocation = ({c: round(v / retirement_balance, 4) for c, v in weighted.items()}
                  if retirement_balance > 0 else fallback)
    if allocation.get("cash", 0) >= 0.05:
        warnings.append(f"{allocation['cash'] * 100:.0f}% of retirement money "
                        f"(${allocation['cash'] * retirement_balance:,.0f}) is in cash or money market; "
                        f"the projection assumes it stays there")

    savings = trailing_savings(db_path, today)
    pulled = (status["last_ok_at"] or "")[:10]
    sources = {
        "current_balance": f"Monarch snapshot {pulled}: pretax + Roth + HSA + taxable accounts "
                           f"(kids' and cash accounts excluded)",
        "annual_contribution": f"Monarch snapshot income - spending, {savings['start']} to {savings['end']} "
                               f"(net pay basis: misses payroll deferrals and employer match; "
                               f"counts cash build-up as saved)",
        "allocation": f"Monarch holdings {(status['last_full_at'] or '')[:10]}, balance-weighted across "
                      f"{len(detail)} retirement accounts; capital-market assumptions as of {rm.load_cma()['as_of']}",
    }
    return {
        "as_of": today.isoformat(),
        "snapshot": {"last_ok_at": status["last_ok_at"], "last_full_at": status["last_full_at"],
                     "stale": status["stale"]},
        "values": {"current_balance": retirement_balance, "annual_contribution": savings["savings"],
                   "allocation": allocation},
        "sources": sources,
        "buckets": {b: round(v, 2) for b, v in buckets.items() if b != "exclude"},
        "trailing_savings": savings,
        "accounts": detail,
        "warnings": warnings,
    }


def _idle_cash_scenario(allocation: dict) -> dict | None:
    """A what-if that moves the retirement accounts' cash into stocks at today's US/intl split."""
    cash = allocation.get("cash", 0.0)
    if cash < 0.05:
        return None
    eq = allocation["us_equity"] + allocation["intl_equity"]
    us_share = allocation["us_equity"] / eq if eq > 0 else 0.7
    new = {"us_equity": allocation["us_equity"] + cash * us_share,
           "intl_equity": allocation["intl_equity"] + cash * (1 - us_share),
           "bonds": allocation["bonds"], "cash": 0.0}
    return {"name": "Idle cash invested", "allocation": {c: round(v, 4) for c, v in new.items()}}


def resolve_assumptions(db_path=None, today: date | None = None, overrides: dict | None = None,
                        extra_scenarios: list[dict] | None = None) -> dict:
    """The complete engine input: stored choices + derived values + explicit overrides.
    Returns {"assumptions", "sources", "inputs"}."""
    stored = get_assumptions(db_path)
    inputs = derive_inputs(db_path, today)
    v = inputs["values"]
    a = {k: stored[k] for k in rm.REQUIRED_KEYS if stored.get(k) is not None}
    a["current_balance"] = v["current_balance"]
    a["annual_contribution"] = (stored["contribution_override"] if stored.get("contribution_override") is not None
                                else v["annual_contribution"])
    a["allocation"] = stored.get("allocation_override") or v["allocation"]
    a["glide_path"] = stored.get("glide_path", DEFAULT_GLIDE)
    if stored.get("fee") is not None:
        a["fee"] = stored["fee"]
    sources = {k: f"config ({ASSUMPTIONS_KEY})" for k in a}
    sources.update(inputs["sources"])
    if stored.get("contribution_override") is not None:
        sources["annual_contribution"] = "config contribution_override"
    if stored.get("allocation_override"):
        sources["allocation"] = "config allocation_override"
    if "glide_path" not in stored:
        sources["glide_path"] = "default (typical target-date glide: 60% stocks at retirement, 40% at plan end)"

    for k, val in (overrides or {}).items():
        a[k] = val
        sources[k] = "user override (this run)"

    scenarios = list(stored.get("scenarios") or [])
    idle = _idle_cash_scenario(a["allocation"]) if a.get("allocation") else None
    if idle and not any(s.get("name") == idle["name"] for s in scenarios):
        scenarios.append(idle)
    scenarios.extend(extra_scenarios or [])
    a["scenarios"] = scenarios

    problems = rm.validate(a)
    if problems:
        raise RetirementError("; ".join(problems))
    return {"assumptions": a, "sources": sources, "inputs": inputs}


# ---------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------


def _summary(res: dict) -> dict:
    return {
        "success_probability": res["success_probability"],
        "success_se": res["success_se"],
        "ending_balance_real_p10": round(res["ending_balance_real"]["p10"]),
        "ending_balance_real_p50": round(res["ending_balance_real"]["p50"]),
        "median_depletion_age": res["depletion"]["median_depletion_age"],
    }


def _slug(tag: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (tag or "").lower()).strip("-")


def _unique_tag(out: Path, as_of: str, tag: str) -> str:
    """The tag itself if no run with it exists for the date, else tag-2, tag-3, ... (or 2, 3, ...
    with no tag), so a re-run never overwrites an earlier run's files."""
    def taken(t: str) -> bool:
        return (out / f"run-{as_of}{'-' + t if t else ''}.json").exists()
    if not taken(tag):
        return tag
    n = 2
    while taken(f"{tag}-{n}" if tag else str(n)):
        n += 1
    return f"{tag}-{n}" if tag else str(n)


def run(db_path=None, today: date | None = None, n_paths: int = 10_000, seed: int | None = 42,
        overrides: dict | None = None, extra_scenarios: list[dict] | None = None,
        tag: str = "", out_dir: Path | str | None = None) -> dict:
    """Resolve inputs, run the engine, and write assumptions/result/run files. Returns a compact
    summary (the full result is in the result file). Never overwrites an earlier run: if the
    date+tag is taken, a numeric suffix is added; the summary's "tag" is the one used."""
    today = _today(today)
    resolved = resolve_assumptions(db_path, today, overrides, extra_scenarios)
    a = resolved["assumptions"]
    result = rm.run_all(a, n_paths=n_paths, seed=seed)
    out = Path(out_dir) if out_dir else RUN_DIR
    out.mkdir(parents=True, exist_ok=True)
    tag = _unique_tag(out, today.isoformat(), _slug(tag))
    stem = today.isoformat() + (f"-{tag}" if tag else "")
    files = {k: out / f"{k}-{stem}.json" for k in ("assumptions", "result", "run")}
    files["assumptions"].write_text(json.dumps(a, indent=2), encoding="utf-8")
    files["result"].write_text(json.dumps(result), encoding="utf-8")
    base = result["base"]
    summary = {
        "as_of": today.isoformat(),
        "tag": tag,
        "files": {k: str(p.relative_to(REPO_ROOT)) if p.is_relative_to(REPO_ROOT) else str(p)
                  for k, p in files.items()},
        "base": _summary(base),
        "returns": base["returns"],
        "scenarios": [{"name": s["name"], **_summary(s),
                       "delta_pp": round(s["success_probability"] - base["success_probability"], 1)}
                      for s in result["scenarios"]],
        "inputs": {"current_balance": a["current_balance"], "annual_contribution": a["annual_contribution"],
                   "allocation": a["allocation"], "glide_path": a.get("glide_path"),
                   "buckets": resolved["inputs"]["buckets"]},
        "sources": resolved["sources"],
        "warnings": resolved["inputs"]["warnings"],
        "accounts": resolved["inputs"]["accounts"],
        "snapshot": resolved["inputs"]["snapshot"],
    }
    files["run"].write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def load_run(as_of: str, tag: str = "", run_dir: Path | str | None = None) -> tuple[dict, dict, dict]:
    """(summary, assumptions, result) for a saved run."""
    d = Path(run_dir) if run_dir else RUN_DIR
    stem = as_of + (f"-{_slug(tag)}" if _slug(tag) else "")
    try:
        return tuple(json.loads((d / f"{k}-{stem}.json").read_text(encoding="utf-8"))
                     for k in ("run", "assumptions", "result"))
    except FileNotFoundError:
        raise RetirementError(f"no saved run for {stem}; run it first", code=1) from None


def workbook_payload(as_of: str, headline: str, body: str, tag: str = "", run_dir=None) -> dict:
    """The retirement_workbook.py payload for a saved run, so nothing is hand-assembled."""
    summary, a, result = load_run(as_of, tag, run_dir)
    data_sources = [
        {"item": "Retirement balance", "value": f"${a['current_balance']:,.0f}",
         "source": summary["sources"].get("current_balance", ""), "pulled": as_of,
         "overridden": "Yes" if "override" in summary["sources"].get("current_balance", "") else "No"},
        {"item": "Annual contribution", "value": f"${a['annual_contribution']:,.0f}",
         "source": summary["sources"].get("annual_contribution", ""), "pulled": as_of,
         "overridden": "Yes" if "override" in summary["sources"].get("annual_contribution", "") else "No"},
        {"item": "Allocation (stocks / bonds / cash)",
         "value": "{:.0f}% / {:.0f}% / {:.0f}%".format(
             (a["allocation"]["us_equity"] + a["allocation"]["intl_equity"]) * 100,
             a["allocation"]["bonds"] * 100, a["allocation"]["cash"] * 100) if a.get("allocation") else "scalar",
         "source": summary["sources"].get("allocation", ""), "pulled": as_of,
         "overridden": "Yes" if "override" in summary["sources"].get("allocation", "") else "No"},
    ]
    for b, v in summary["inputs"]["buckets"].items():
        data_sources.append({"item": f"Bucket: {b}", "value": f"${v:,.0f}", "source": "Monarch snapshot",
                             "pulled": as_of, "overridden": "No"})
    for w in summary["warnings"]:
        data_sources.append({"item": "Warning", "value": "", "source": w, "pulled": as_of, "overridden": ""})
    return {
        "generated_date": as_of,
        "assumptions": a,
        "assumption_sources": summary["sources"],
        "result": result,
        "accounts": summary["accounts"],
        "narrative": {"headline": headline, "body": body},
        "data_sources": data_sources,
    }
