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


def validate(a: dict) -> list[str]:
    """Return a list of problems (empty = valid)."""
    problems = [f"missing assumption key: {k}" for k in REQUIRED_KEYS if a.get(k) is None]
    if problems:
        return problems
    ca, ra, pa = (int(a[k]) for k in ("current_age", "retirement_age", "plan_through_age"))
    if not (18 <= ca <= ra <= pa <= 120):
        problems.append("ages must satisfy 18 <= current <= retirement <= plan_through <= 120")
    for k in ("current_balance", "annual_contribution", "retirement_spend", "ss_annual_amount"):
        if float(a[k]) < 0:
            problems.append(f"{k} must not be negative")
    if not (-0.02 <= float(a["inflation"]) <= 0.15):
        problems.append("inflation must be between -2% and 15%")
    if float(a["ss_annual_amount"]) > 0 and not (62 <= int(a["ss_start_age"]) <= 70):
        problems.append("ss_start_age must be between 62 and 70")
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


def _cashflows(a: dict, k: int) -> tuple[float, float, float, float]:
    """(contribution, withdrawal, social security, net_out) for year k in nominal dollars."""
    age = int(a["current_age"]) + k
    infl = (1.0 + float(a["inflation"])) ** k
    contrib = float(a["annual_contribution"]) * infl if age < int(a["retirement_age"]) else 0.0
    withdrawal = float(a["retirement_spend"]) * infl if age >= int(a["retirement_age"]) else 0.0
    ss = float(a["ss_annual_amount"]) * infl if age >= int(a["ss_start_age"]) else 0.0
    return contrib, withdrawal, ss, withdrawal - ss


def _echo(a: dict) -> dict:
    out = {k: a[k] for k in REQUIRED_KEYS}
    out.update({k: a[k] for k in OPTIONAL_KEYS if a.get(k) is not None})
    return out


def run_simulation(assumptions: dict, n_paths: int = 10_000, seed: int | None = None) -> dict:
    """Run the Monte Carlo projection and return a result dict."""
    a = assumptions
    _validate(a)
    rng = np.random.default_rng(seed)
    sched = return_schedule(a)

    ca, ra, pa = int(a["current_age"]), int(a["retirement_age"]), int(a["plan_through_age"])
    inflation = float(a["inflation"])
    balance = np.full(n_paths, float(a["current_balance"]))
    depleted = np.zeros(n_paths, dtype=bool)
    depletion_age = np.full(n_paths, np.nan)
    trajectory = [balance.copy()]  # index i corresponds to age ca + i (start of that year)

    for k in range(pa - ca):
        age = ca + k
        contrib, _w, _ss, net_out = _cashflows(a, k)
        balance = balance - net_out
        if age >= ra:
            newly = (~depleted) & (balance <= 0.0)
            depletion_age[newly] = age
            depleted |= balance <= 0.0
        balance = np.maximum(balance, 0.0)
        growth = _lognormal_factors(rng, sched[k]["mean"], sched[k]["vol"], n_paths)
        balance = balance * growth + contrib * np.sqrt(growth)
        balance = np.where(depleted, 0.0, balance)
        trajectory.append(balance.copy())

    p = float(np.mean(~depleted))
    percentiles, percentiles_real = [], []
    for i, col in enumerate(trajectory):
        deflator = (1.0 + inflation) ** i
        q10, q50, q90 = (float(np.percentile(col, q)) for q in (10, 50, 90))
        percentiles.append({"age": ca + i, "p10": q10, "p50": q50, "p90": q90})
        percentiles_real.append({"age": ca + i, "p10": q10 / deflator, "p50": q50 / deflator,
                                 "p90": q90 / deflator})

    fail_ages = depletion_age[~np.isnan(depletion_age)]
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
        "returns": {
            "now": sched[0] if sched else None,
            "at_retirement": sched[ret_idx] if sched else None,
            "at_end": sched[-1] if sched else None,
        },
        "assumptions": _echo(a),
    }


def deterministic_projection(assumptions: dict, schedule: list[dict] | None = None) -> list[dict]:
    """Single median-growth year-by-year projection, one row per year. The workbook's Base
    Case reproduces it with live formulas, using each row's median_return."""
    a = assumptions
    sched = schedule or return_schedule(a)
    ca, pa = int(a["current_age"]), int(a["plan_through_age"])
    inflation = float(a["inflation"])
    rows = []
    balance = float(a["current_balance"])
    for k in range(pa - ca):
        contrib, withdrawal, ss, net_out = _cashflows(a, k)
        g = median_factor(sched[k]["mean"], sched[k]["vol"])
        start = balance
        after_out = start - net_out
        growth = after_out * (g - 1.0) + contrib * (math.sqrt(g) - 1.0)
        balance = max(0.0, after_out + growth + contrib)
        rows.append({
            "age": ca + k,
            "start_balance": round(start, 2),
            "contribution": round(contrib, 2),
            "median_return": round(g - 1.0, 12),
            "growth": round(growth, 2),
            "withdrawal": round(withdrawal, 2),
            "ss": round(ss, 2),
            "end_balance": round(balance, 2),
            "end_balance_real": round(balance / (1.0 + inflation) ** (k + 1), 2),
        })
    return rows


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
