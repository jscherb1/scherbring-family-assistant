#!/usr/bin/env python3
"""Monte Carlo retirement-projection engine (Phase 3 of the personal-finance
program). Pure computation: it never talks to Monarch or Drive. The shared
service (scripts/retirement.py) resolves the assumptions dict from the local
Monarch snapshot and config and hands it in, so this stays deterministic under
a seed and unit-testable.

Usage (CLI):
    python scripts/retirement_model.py --data-file <assumptions.json> \
        [--paths 10000] [--seed 42] [--out <result.json>]

The assumptions JSON has this shape:
{
  "current_age": 37,
  "retirement_age": 62,
  "plan_through_age": 95,
  "current_balance": 500000,
  "annual_contribution": 40000,      # today's dollars, grows with inflation
  "retirement_spend": 120000,        # today's dollars, grows with inflation
  "inflation": 0.03,
  "ss_start_age": 67,                # 62-70
  "ss_annual_amount": 40000,         # today's dollars, grows with inflation

  # Returns: EITHER an allocation priced with the capital-market assumptions
  # (scripts/retirement_data/cma.json) ...
  "allocation": {"us_equity": 0.55, "intl_equity": 0.18, "bonds": 0.27, "cash": 0.0},
  "glide_path": {"retirement_equity": 0.60, "end_equity": 0.40},   # optional
  "fee": 0.0015,                                                   # optional
  # ... OR scalar returns (used when "allocation" is absent or null):
  "expected_return": 0.075,          # nominal arithmetic mean
  "volatility": 0.13,                # annual stddev

  "scenarios": [{"name": "Retire at 60", "retirement_age": 60}]    # optional
}

Cash-flow convention for the year starting at age a (k = a - current_age,
infl = (1 + inflation)**k, g = that year's growth factor):
    net_out = spend*infl (if a >= retirement_age) - ss*infl (if a >= ss_start_age)
    growth  = (start - net_out) * (g - 1) + contribution*infl*(sqrt(g) - 1)
    end     = max(0, start - net_out + growth + contribution*infl)
Withdrawals come out at the start of the year (conservative; captures sequence
risk) and contributions arrive mid-year (sqrt(g) = half a year of growth).
A path is depleted the first year start - net_out <= 0 in retirement.

The deterministic projection uses the MEDIAN growth factor of the lognormal,
(1 + m) / sqrt(1 + (s / (1 + m))**2), not the arithmetic mean, so it really is
the middle path. The workbook's Base Case reproduces it with live formulas.

Prints JSON to stdout: {"base": <result>, "scenarios": [<result>, ...]}.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

import retirement_tax as rt

REPO_ROOT = Path(__file__).resolve().parent.parent
CMA_PATH = Path(__file__).resolve().parent / "retirement_data" / "cma.json"

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

# Legacy stock/bond buckets -> (nominal arithmetic return, volatility). Superseded by the
# capital-market assumptions for the base case; kept for scalar what-ifs and old callers.
ALLOCATION_RETURN_VOL = {
    90: (0.080, 0.15),
    80: (0.075, 0.13),
    60: (0.060, 0.10),
    40: (0.045, 0.07),
}

ASSET_CLASSES = ("us_equity", "intl_equity", "bonds", "cash")
EQUITY = ("us_equity", "intl_equity")

REQUIRED_KEYS = (
    "current_age",
    "retirement_age",
    "plan_through_age",
    "current_balance",
    "annual_contribution",
    "retirement_spend",
    "inflation",
    "ss_start_age",
    "ss_annual_amount",
)
OPTIONAL_KEYS = ("allocation", "glide_path", "fee", "expected_return", "volatility")


def returns_for_allocation(stock_pct: float) -> tuple[float, float]:
    """Map a stock percentage to (expected_return, volatility) by nearest legacy bucket."""
    nearest = min(ALLOCATION_RETURN_VOL, key=lambda k: abs(k - stock_pct))
    return ALLOCATION_RETURN_VOL[nearest]


def load_cma(path: Path | str | None = None) -> dict:
    return json.loads(Path(path or CMA_PATH).read_text(encoding="utf-8"))


def _cma_arrays(cma: dict) -> tuple[np.ndarray, np.ndarray]:
    """(arithmetic means, covariance matrix) in ASSET_CLASSES order."""
    classes = cma["asset_classes"]
    vol = np.array([classes[c]["volatility"] for c in ASSET_CLASSES])
    mean = np.array([classes[c]["geometric"] + classes[c]["volatility"] ** 2 / 2 for c in ASSET_CLASSES])
    corr = np.eye(len(ASSET_CLASSES))
    for key, rho in (cma.get("correlations") or {}).items():
        a, b = key.split("|")
        i, j = ASSET_CLASSES.index(a), ASSET_CLASSES.index(b)
        corr[i, j] = corr[j, i] = rho
    return mean, np.outer(vol, vol) * corr


def _weights_at(a: dict, k: int) -> np.ndarray:
    """Allocation weights for year k, following the glide path. Equity share moves linearly
    from today's to `retirement_equity` at retirement, then to `end_equity` at the plan end;
    the US/international split and the bonds/cash split stay in today's proportions."""
    alloc = a["allocation"]
    w0 = np.array([float(alloc.get(c, 0.0)) for c in ASSET_CLASSES])
    e0 = w0[0] + w0[1]
    glide = a.get("glide_path") or {}
    n_ret = int(a["retirement_age"]) - int(a["current_age"])
    n_end = int(a["plan_through_age"]) - int(a["current_age"])
    e_ret = float(glide.get("retirement_equity", e0))
    e_end = float(glide.get("end_equity", e_ret))
    if k <= n_ret:
        e = e0 + (e_ret - e0) * (k / n_ret if n_ret else 1.0)
    else:
        span = n_end - n_ret
        e = e_ret + (e_end - e_ret) * ((k - n_ret) / span if span else 1.0)
    us_share = w0[0] / e0 if e0 > 0 else 0.7
    bond_share = w0[2] / (w0[2] + w0[3]) if (w0[2] + w0[3]) > 0 else 1.0
    return np.array([e * us_share, e * (1 - us_share), (1 - e) * bond_share, (1 - e) * (1 - bond_share)])


def return_schedule(a: dict, cma: dict | None = None) -> list[dict]:
    """Per-year (arithmetic mean return net of fees, volatility, equity share), one entry per
    simulated year. Scalar mode repeats expected_return/volatility every year."""
    n = int(a["plan_through_age"]) - int(a["current_age"])
    if not a.get("allocation"):
        m, s = float(a["expected_return"]), float(a["volatility"])
        return [{"mean": m, "vol": s, "equity": None} for _ in range(n)]
    cma = cma or a.get("cma") or load_cma()
    mean, cov = _cma_arrays(cma)
    fee = float(a.get("fee", cma.get("default_fee", 0.0)))
    out = []
    for k in range(n):
        w = _weights_at(a, k)
        out.append({"mean": float(w @ mean) - fee, "vol": float(math.sqrt(w @ cov @ w)),
                    "equity": float(w[0] + w[1])})
    return out


def median_factor(mean: float, vol: float) -> float:
    """Median of the lognormal growth factor with arithmetic mean 1+mean and stddev vol."""
    m = 1.0 + mean
    return m / math.sqrt(1.0 + (vol / m) ** 2) if vol > 0 else m


def _lognormal_factors(rng, mean_return: float, volatility: float, size) -> np.ndarray:
    """Draw multiplicative annual growth factors (1 + return) from a lognormal with
    arithmetic mean (1 + mean_return) and stddev `volatility`. Volatility 0 yields the
    constant factor (1 + mean_return)."""
    m = 1.0 + mean_return
    if volatility <= 0:
        return np.full(size, m)
    sigma2 = math.log(1.0 + (volatility / m) ** 2)
    mu = math.log(m) - 0.5 * sigma2
    return rng.lognormal(mean=mu, sigma=math.sqrt(sigma2), size=size)


HOUSEHOLD_KEYS = ("buckets", "contributions", "contribution_real_growth", "people", "events", "kids_balance",
                  "kids_contribution", "taxable_gain_fraction", "tax", "withdrawal_order", "ss_factor", "survivor")
BUCKETS = ("taxable", "pretax", "roth")
DEFAULT_ORDER = ("taxable", "pretax", "roth")


def _is_household(a: dict) -> bool:
    return a.get("buckets") is not None


def validate(a: dict) -> list[str]:
    """Return a list of problems (empty = valid)."""
    household = _is_household(a)
    required = [k for k in REQUIRED_KEYS
                if not (household and k in ("current_balance", "annual_contribution", "ss_start_age",
                                             "ss_annual_amount"))]
    problems = [f"missing assumption key: {k}" for k in required if a.get(k) is None]
    if problems:
        return problems
    ca, ra, pa = (int(a[k]) for k in ("current_age", "retirement_age", "plan_through_age"))
    if not (18 <= ca <= ra <= pa <= 120):
        problems.append("ages must satisfy 18 <= current <= retirement <= plan_through <= 120")
    for k in ("current_balance", "annual_contribution", "retirement_spend", "ss_annual_amount"):
        if a.get(k) is not None and float(a[k]) < 0:
            problems.append(f"{k} must not be negative")
    if not (-0.02 <= float(a["inflation"]) <= 0.15):
        problems.append("inflation must be between -2% and 15%")
    if not household and float(a["ss_annual_amount"]) > 0 and not (62 <= int(a["ss_start_age"]) <= 70):
        problems.append("ss_start_age must be between 62 and 70")
    if household:
        for key in ("buckets", "contributions"):
            d = a.get(key) or {}
            if set(d) - set(BUCKETS):
                problems.append(f"{key} keys must be among {', '.join(BUCKETS)}")
            if any(float(v) < 0 for v in d.values()):
                problems.append(f"{key} must not be negative")
        order = a.get("withdrawal_order") or DEFAULT_ORDER
        if sorted(order) != sorted(BUCKETS):
            problems.append("withdrawal_order must list taxable, pretax and roth once each")
        for p in a.get("people") or []:
            claim = p.get("ss_claim_age", 67)
            if not (62 <= float(claim) <= 70):
                problems.append(f"{p.get('name', 'person')}: ss_claim_age must be between 62 and 70")
        for e in a.get("events") or []:
            if e.get("amount") is None or e.get("start_age") is None:
                problems.append(f"event {e.get('name', '?')}: needs amount and start_age")
        if not (0.0 <= float(a.get("taxable_gain_fraction", 0.5)) <= 1.0):
            problems.append("taxable_gain_fraction must be between 0 and 1")
    alloc = a.get("allocation")
    if alloc:
        unknown = set(alloc) - set(ASSET_CLASSES)
        if unknown:
            problems.append(f"unknown allocation classes: {sorted(unknown)}")
        if any(float(v) < 0 for v in alloc.values()):
            problems.append("allocation weights must not be negative")
        if abs(sum(float(v) for v in alloc.values()) - 1.0) > 0.01:
            problems.append("allocation weights must sum to 1")
        for k, v in (a.get("glide_path") or {}).items():
            if k not in ("retirement_equity", "end_equity"):
                problems.append(f"unknown glide_path key: {k}")
            elif not (0.0 <= float(v) <= 1.0):
                problems.append(f"glide_path.{k} must be between 0 and 1")
        if not (0.0 <= float(a.get("fee", 0.0)) <= 0.03):
            problems.append("fee must be between 0% and 3%")
    else:
        for k in ("expected_return", "volatility"):
            if a.get(k) is None:
                problems.append(f"missing {k} (or give an allocation)")
        if a.get("volatility") is not None and float(a["volatility"]) < 0:
            problems.append("volatility must not be negative")
    return problems


def _validate(a: dict) -> None:
    problems = validate(a)
    if problems:
        raise ValueError("; ".join(problems))


# ---------------------------------------------------------------------------
# household model
# ---------------------------------------------------------------------------


def _resolve_age(value, a: dict) -> int:
    """Event ages may be ints or the words 'retirement' / 'plan_end'."""
    if value == "retirement":
        return int(a["retirement_age"])
    if value == "plan_end":
        return int(a["plan_through_age"])
    return int(value)


def normalize(a: dict, tax: dict | None = None) -> dict:
    """The household form the core runs on. A simple (legacy) assumptions dict becomes one
    taxable bucket, one fixed Social Security benefit and no taxes, which reproduces the
    single-pool model exactly."""
    ca, ra = int(a["current_age"]), int(a["retirement_age"])
    if _is_household(a):
        buckets = {b: float((a.get("buckets") or {}).get(b, 0.0)) for b in BUCKETS}
        contributions = {b: float((a.get("contributions") or {}).get(b, 0.0)) for b in BUCKETS}
        people = list(a.get("people") or [])
    else:
        buckets = {"taxable": float(a["current_balance"]), "pretax": 0.0, "roth": 0.0}
        contributions = {"taxable": float(a["annual_contribution"]), "pretax": 0.0, "roth": 0.0}
        people = [{"name": "household", "age_offset": 0, "ss_benefit": float(a["ss_annual_amount"]),
                   "ss_claim_age": int(a["ss_start_age"])}]
    tax_cfg = a.get("tax")
    tax_on = bool(tax_cfg) and (not isinstance(tax_cfg, dict) or tax_cfg.get("enabled", True))
    tax = tax or (rt.load_tax() if (tax_on or any("ss_pia" in p for p in people)) else None)
    fra = tax["social_security"]["full_retirement_age"] if tax else 67

    resolved_people = []
    for p in people:
        claim = float(p.get("ss_claim_age", 67))
        if "ss_pia" in p:
            years = min(ra, fra) + int(p.get("age_offset", 0)) - float(p.get("work_start_age", 22))
            pia = rt.effective_pia(float(p["ss_pia"]) / 12, years, tax) * 12
            benefit = pia * rt.claim_factor(claim, fra)
        else:
            benefit = float(p.get("ss_benefit", 0.0))
        resolved_people.append({"name": p.get("name", "person"), "age_offset": int(p.get("age_offset", 0)),
                                "claim_age": claim, "benefit": benefit})

    events = []
    for e in a.get("events") or []:
        start = _resolve_age(e["start_age"], a)
        if e.get("min_age") is not None:
            start = max(start, int(e["min_age"]))
        events.append({"name": e.get("name", "event"), "start": start,
                       "end": _resolve_age(e.get("end_age", e["start_age"]), a), "amount": float(e["amount"]),
                       "real": bool(e.get("real", True)), "real_growth": float(e.get("real_growth", 0.0)),
                       "kids": e.get("funding") == "kids"})
    kids_end = max([e["end"] for e in events if e["kids"]], default=ca - 1)
    surv = a.get("survivor") or {}
    return {
        "ca": ca, "ra": ra, "pa": int(a["plan_through_age"]), "inflation": float(a["inflation"]),
        "spend": float(a["retirement_spend"]), "buckets": buckets, "contributions": contributions,
        "contribution_growth": float(a.get("contribution_real_growth", 0.0)),
        "people": resolved_people, "ss_factor": float(a.get("ss_factor", 1.0)),
        "events": events, "kids_balance": float(a.get("kids_balance", 0.0)),
        "kids_contribution": float(a.get("kids_contribution", 0.0)), "kids_contribution_end": kids_end,
        "gain_fraction": float(a.get("taxable_gain_fraction", 0.5)),
        "order": tuple(a.get("withdrawal_order") or DEFAULT_ORDER),
        "tax": tax if tax_on else None, "state": (tax_cfg or {}).get("state", "MN") if isinstance(tax_cfg, dict) else "MN",
        "first_death_age": surv.get("first_death_age"), "survivor_spend": float(surv.get("spend_factor", 0.75)),
    }


def _draw(bal: dict, need, order, rmd):
    """Withdraw `need` (array) from the buckets in order, taking at least `rmd` from pre-tax.
    Returns (draws per bucket, surplus to reinvest in taxable, shortfall)."""
    d = {b: np.zeros_like(need) for b in BUCKETS}
    d["pretax"] = np.minimum(bal["pretax"], rmd)
    remaining = need - d["pretax"]
    surplus = np.maximum(-remaining, 0.0)
    remaining = np.maximum(remaining, 0.0)
    for b in order:
        take = np.minimum(bal[b] - d[b], remaining)
        d[b] = d[b] + take
        remaining = remaining - take
    return d, surplus, remaining


def _project(hh: dict, growth_for_year, n: int, record: bool = False) -> dict:
    """Run the household cash flows over n paths. growth_for_year(k) gives that year's growth
    factors (shape n). Withdrawals at the start of the year, contributions mid-year."""
    tax = hh["tax"]
    ca, ra, pa, i = hh["ca"], hh["ra"], hh["pa"], hh["inflation"]
    bal = {b: np.full(n, v) for b, v in hh["buckets"].items()}
    kids = np.full(n, hh["kids_balance"])
    depleted = np.zeros(n, dtype=bool)
    depletion_age = np.full(n, np.nan)
    lifetime_tax_real = np.zeros(n)
    totals = [sum(bal.values()).copy()]
    rows = []
    divisors = (tax or rt.load_tax())["rmd"]["uniform_lifetime_divisors"] if tax else {}
    rmd_start = tax["rmd"]["start_age"] if tax else 999
    penalty = tax["early_withdrawal"] if tax else None
    working = tax["working_years"] if tax else None

    for k in range(pa - ca):
        age = ca + k
        infl = (1.0 + i) ** k
        retired = age >= ra
        widowed = hh["first_death_age"] is not None and age >= hh["first_death_age"]

        benefits = [p["benefit"] for p in hh["people"] if age + p["age_offset"] >= p["claim_age"]]
        ss = (max(benefits, default=0.0) if widowed else sum(benefits)) * infl * hh["ss_factor"]
        spend = hh["spend"] * infl if retired else 0.0
        other = -(1.0 - hh["survivor_spend"]) * spend if widowed else 0.0
        kids_need = 0.0
        for e in hh["events"]:
            if e["start"] <= age <= e["end"]:
                amt = e["amount"] * (infl if e["real"] else 1.0) * (1.0 + e["real_growth"]) ** k
                if e["kids"]:
                    kids_need += amt
                else:
                    other += amt
        from_kids = np.minimum(kids, kids_need)
        kids = kids - from_kids
        other = other + (kids_need - from_kids)
        need = spend + other - ss

        rmd = bal["pretax"] / divisors[str(min(age, 100))] if (tax and age >= rmd_start) else np.zeros(n)
        taxes = np.zeros(n)
        seniors = sum(1 for p in hh["people"] if age + p["age_offset"] >= 65)
        status = "single" if (widowed or len(hh["people"]) < 2) else "mfj"
        for _ in range(8 if tax else 1):
            d, surplus, short = _draw(bal, need + taxes, hh["order"], rmd)
            if not tax:
                break
            gains = d["taxable"] * hh["gain_fraction"]
            if retired:
                new = rt.income_tax(d["pretax"], gains, np.full(n, ss), infl, status=status,
                                    seniors=min(seniors, 1 if widowed else 2), state=hh["state"], tax=tax)["total"]
                if age < penalty["penalty_free_age"]:
                    new = new + penalty["penalty"] * d["pretax"]
            else:
                new = gains * working["gains_rate"] + d["pretax"] * working["pretax_rate"]
            if np.allclose(new, taxes, atol=0.5):
                taxes = new
                d, surplus, short = _draw(bal, need + taxes, hh["order"], rmd)
                break
            taxes = new

        start_total = sum(bal.values())
        for b in BUCKETS:
            bal[b] = bal[b] - d[b]
        bal["taxable"] = bal["taxable"] + surplus
        newly = (~depleted) & (short > 1e-6)
        depletion_age[newly] = age
        depleted |= short > 1e-6
        lifetime_tax_real += taxes / infl

        g = growth_for_year(k)
        cg = infl * (1.0 + hh["contribution_growth"]) ** k
        contrib = {b: (hh["contributions"][b] * cg if not retired else 0.0) for b in BUCKETS}
        after = sum(bal.values())
        for b in BUCKETS:
            bal[b] = np.where(depleted, 0.0, bal[b] * g + contrib[b] * np.sqrt(g))
        kc = hh["kids_contribution"] * infl if age <= hh["kids_contribution_end"] and not retired else 0.0
        kids = kids * g + kc * np.sqrt(g)
        totals.append(sum(bal.values()).copy())

        if record:
            c_total = sum(contrib.values())
            end = float(totals[-1][0])
            rows.append({
                "age": age,
                "start_balance": round(float(start_total[0]), 2),
                "contribution": round(c_total, 2),
                "median_return": round(float(g[0]) - 1.0, 12),
                "growth": round(float(after[0]) * (float(g[0]) - 1.0) + c_total * (math.sqrt(float(g[0])) - 1.0), 2),
                "withdrawal": round(spend, 2),
                "ss": round(ss, 2),
                "other_outflows": round(float(np.asarray(other)[0] if np.ndim(other) else other), 2),
                "taxes": round(float(taxes[0]), 2),
                "end_balance": round(end, 2),
                "end_balance_real": round(end / (1.0 + i) ** (k + 1), 2),
                "buckets": {b: round(float(bal[b][0]), 2) for b in BUCKETS},
                "kids_balance": round(float(kids[0]), 2),
            })
    return {"totals": totals, "depleted": depleted, "depletion_age": depletion_age,
            "lifetime_tax_real": lifetime_tax_real, "kids": kids, "rows": rows}


def _echo(a: dict) -> dict:
    out = {k: a[k] for k in REQUIRED_KEYS if a.get(k) is not None}
    out.update({k: a[k] for k in OPTIONAL_KEYS + HOUSEHOLD_KEYS if a.get(k) is not None})
    return out


def run_simulation(assumptions: dict, n_paths: int = 10_000, seed: int | None = None) -> dict:
    """Run the Monte Carlo projection and return a result dict."""
    a = assumptions
    _validate(a)
    rng = np.random.default_rng(seed)
    sched = return_schedule(a)
    hh = normalize(a)
    out = _project(hh, lambda k: _lognormal_factors(rng, sched[k]["mean"], sched[k]["vol"], n_paths), n_paths)

    ca, ra, inflation = hh["ca"], hh["ra"], hh["inflation"]
    depleted = out["depleted"]
    p = float(np.mean(~depleted))
    percentiles, percentiles_real = [], []
    for idx, col in enumerate(out["totals"]):
        deflator = (1.0 + inflation) ** idx
        q10, q50, q90 = (float(np.percentile(col, q)) for q in (10, 50, 90))
        percentiles.append({"age": ca + idx, "p10": q10, "p50": q50, "p90": q90})
        percentiles_real.append({"age": ca + idx, "p10": q10 / deflator, "p50": q50 / deflator,
                                 "p90": q90 / deflator})

    fail_ages = out["depletion_age"][~np.isnan(out["depletion_age"])]
    ret_idx = min(max(ra - ca, 0), len(sched) - 1) if sched else 0
    return {
        "success_probability": round(p * 100.0, 1),
        "success_se": round(math.sqrt(p * (1 - p) / n_paths) * 100.0, 2),
        "n_paths": n_paths,
        "percentiles": percentiles,
        "percentiles_real": percentiles_real,
        "deterministic": deterministic_projection(a, sched),
        "depletion": {
            "failure_rate": round(100.0 - p * 100.0, 1),
            "median_depletion_age": float(np.median(fail_ages)) if fail_ages.size else None,
        },
        "ending_balance": {k: percentiles[-1][k] for k in ("p10", "p50", "p90")},
        "ending_balance_real": {k: percentiles_real[-1][k] for k in ("p10", "p50", "p90")},
        "lifetime_taxes_real_p50": round(float(np.median(out["lifetime_tax_real"])), 2),
        "social_security": [{"name": pp["name"], "annual_benefit_today": round(pp["benefit"], 2),
                             "claim_age": pp["claim_age"]} for pp in hh["people"]],
        "returns": {
            "now": sched[0] if sched else None,
            "at_retirement": sched[ret_idx] if sched else None,
            "at_end": sched[-1] if sched else None,
        },
        "assumptions": _echo(a),
    }


def deterministic_projection(assumptions: dict, schedule: list[dict] | None = None) -> list[dict]:
    """Single median-growth year-by-year projection through the same household core as the
    simulation, one row per year. The workbook's Base Case reproduces each row's end balance
    with live formulas from its contribution, spending, Social Security, other outflows,
    taxes and median return."""
    a = assumptions
    sched = schedule or return_schedule(a)
    hh = normalize(a)
    out = _project(hh, lambda k: np.array([median_factor(sched[k]["mean"], sched[k]["vol"])]), 1, record=True)
    return out["rows"]


def run_all(assumptions: dict, n_paths: int = 10_000, seed: int | None = None) -> dict:
    """Run the base case plus any what-if scenarios. Each scenario is the base assumptions
    with its overrides applied (an override of null removes the key, e.g. "allocation": null
    switches to scalar returns). The base case is never mutated."""
    base = run_simulation(assumptions, n_paths=n_paths, seed=seed)
    scenarios = []
    for sc in assumptions.get("scenarios", []) or []:
        merged = {k: v for k, v in assumptions.items() if k != "scenarios"}
        for k, v in sc.items():
            if k == "name":
                continue
            if v is None:
                merged.pop(k, None)
            else:
                merged[k] = v
        res = run_simulation(merged, n_paths=n_paths, seed=seed)
        res["name"] = sc.get("name", "scenario")
        scenarios.append(res)
    return {"base": base, "scenarios": scenarios}


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Monte Carlo retirement engine")
    ap.add_argument("--data-file", required=True, help="assumptions JSON path")
    ap.add_argument("--paths", type=int, default=10_000)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out", default=None, help="write result JSON here too")
    args = ap.parse_args(argv)

    assumptions = json.loads(Path(args.data_file).read_text(encoding="utf-8"))
    result = run_all(assumptions, n_paths=args.paths, seed=args.seed)
    payload = json.dumps(result, indent=2)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
