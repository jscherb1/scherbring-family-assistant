#!/usr/bin/env python3
"""Monte Carlo retirement-projection engine (Phase 3 of the personal-finance
program). Pure computation — it never talks to Monarch or Drive; the
`retirement` subagent gathers data via the `monarch` MCP tools and hands a
resolved assumptions dict in, so this stays deterministic under a seed and
unit-testable.

Usage (CLI):
    python scripts/retirement_model.py --data-file <assumptions.json> \
        [--paths 10000] [--seed 42] [--out <result.json>]

The assumptions JSON has this shape (all required unless noted):
{
  "current_age": 37,
  "retirement_age": 62,
  "plan_through_age": 95,
  "current_balance": 500000,
  "annual_contribution": 40000,      # today's dollars, grows with inflation
  "retirement_spend": 120000,        # today's dollars, grows with inflation
  "inflation": 0.03,
  "expected_return": 0.075,          # nominal annual
  "volatility": 0.13,                # annual stddev of the growth factor
  "ss_start_age": 67,
  "ss_annual_amount": 40000,         # today's dollars, grows with inflation
  "scenarios": [                     # optional what-if overrides
    {"name": "Retire at 60", "retirement_age": 60}
  ]
}

Cash-flow convention (mirrored by the workbook's live formulas):
    end_balance = start_balance * (1 + return)
                  + contribution*infl        (while age < retirement_age)
                  - (spend - ss)*infl         (while age >= retirement_age)
where infl = (1 + inflation)**k, k = years since today (k=0 => factor 1.0),
and ss applies only once age >= ss_start_age.

Prints JSON to stdout: {"base": <result>, "scenarios": [<result>, ...]}.
Each <result> carries success_probability, percentiles, deterministic, and a
depletion summary. See run_simulation / build_result.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

# Stock/bond allocation -> (nominal expected return, volatility). Documented
# planning assumptions; the agent maps a Monarch-derived stock share to the
# nearest bucket and the user can override the raw return/vol directly.
ALLOCATION_RETURN_VOL = {
    90: (0.080, 0.15),
    80: (0.075, 0.13),
    60: (0.060, 0.10),
    40: (0.045, 0.07),
}

REQUIRED_KEYS = (
    "current_age",
    "retirement_age",
    "plan_through_age",
    "current_balance",
    "annual_contribution",
    "retirement_spend",
    "inflation",
    "expected_return",
    "volatility",
    "ss_start_age",
    "ss_annual_amount",
)


def returns_for_allocation(stock_pct: float) -> tuple[float, float]:
    """Map a stock allocation percentage to (expected_return, volatility) by
    nearest documented bucket."""
    nearest = min(ALLOCATION_RETURN_VOL, key=lambda k: abs(k - stock_pct))
    return ALLOCATION_RETURN_VOL[nearest]


def _lognormal_factors(rng, mean_return: float, volatility: float, size) -> np.ndarray:
    """Draw multiplicative annual growth factors (1 + return) from a lognormal
    with arithmetic mean (1 + mean_return) and stddev `volatility`. Volatility
    of 0 yields the constant factor (1 + mean_return)."""
    m = 1.0 + mean_return
    if volatility <= 0:
        return np.full(size, m)
    sigma2 = math.log(1.0 + (volatility / m) ** 2)
    mu = math.log(m) - 0.5 * sigma2
    return rng.lognormal(mean=mu, sigma=math.sqrt(sigma2), size=size)


def _validate(a: dict) -> None:
    missing = [k for k in REQUIRED_KEYS if k not in a]
    if missing:
        raise ValueError(f"missing assumption keys: {missing}")
    if not (a["current_age"] <= a["retirement_age"] <= a["plan_through_age"]):
        raise ValueError("ages must satisfy current <= retirement <= plan_through")


def run_simulation(assumptions: dict, n_paths: int = 10_000, seed: int | None = None) -> dict:
    """Run the Monte Carlo projection and return a result dict."""
    a = assumptions
    _validate(a)
    rng = np.random.default_rng(seed)

    ca = int(a["current_age"])
    ra = int(a["retirement_age"])
    pa = int(a["plan_through_age"])
    inflation = float(a["inflation"])
    exp_ret = float(a["expected_return"])
    vol = float(a["volatility"])
    contribution = float(a["annual_contribution"])
    spend = float(a["retirement_spend"])
    ss_age = int(a["ss_start_age"])
    ss_amt = float(a["ss_annual_amount"])

    balance = np.full(n_paths, float(a["current_balance"]))
    depleted = np.zeros(n_paths, dtype=bool)
    depletion_age = np.full(n_paths, np.nan)

    ages = list(range(ca, pa + 1))
    trajectory = [balance.copy()]  # index i corresponds to ages[i]

    for k, age in enumerate(range(ca, pa)):  # transition age -> age+1
        infl = (1.0 + inflation) ** k
        growth = _lognormal_factors(rng, exp_ret, vol, n_paths)
        balance = balance * growth
        if age < ra:
            balance = balance + contribution * infl
        else:
            ss = ss_amt * infl if age >= ss_age else 0.0
            balance = balance - (spend * infl - ss)
        newly = (~depleted) & (balance <= 0.0)
        depletion_age[newly] = age + 1
        depleted |= balance <= 0.0
        balance = np.where(depleted, 0.0, balance)
        trajectory.append(balance.copy())

    success = float(np.mean(~depleted)) * 100.0

    percentiles = []
    for i, age in enumerate(ages):
        col = trajectory[i]
        percentiles.append({
            "age": age,
            "p10": float(np.percentile(col, 10)),
            "p50": float(np.percentile(col, 50)),
            "p90": float(np.percentile(col, 90)),
        })

    fail_ages = depletion_age[~np.isnan(depletion_age)]
    median_depletion_age = float(np.median(fail_ages)) if fail_ages.size else None

    return {
        "success_probability": round(success, 1),
        "percentiles": percentiles,
        "deterministic": deterministic_projection(a),
        "depletion": {
            "failure_rate": round(100.0 - success, 1),
            "median_depletion_age": median_depletion_age,
        },
        "ending_balance": {
            "p10": percentiles[-1]["p10"],
            "p50": percentiles[-1]["p50"],
            "p90": percentiles[-1]["p90"],
        },
        "assumptions": {k: a[k] for k in REQUIRED_KEYS},
    }


def deterministic_projection(assumptions: dict) -> list[dict]:
    """Single median-return year-by-year projection. The workbook's Base Case
    tab reproduces this with live Excel formulas referencing the Assumptions
    cells, so the columns here match those formulas exactly."""
    a = assumptions
    ca = int(a["current_age"])
    ra = int(a["retirement_age"])
    pa = int(a["plan_through_age"])
    inflation = float(a["inflation"])
    exp_ret = float(a["expected_return"])
    contribution = float(a["annual_contribution"])
    spend = float(a["retirement_spend"])
    ss_age = int(a["ss_start_age"])
    ss_amt = float(a["ss_annual_amount"])

    rows = []
    balance = float(a["current_balance"])
    for k, age in enumerate(range(ca, pa)):
        infl = (1.0 + inflation) ** k
        start = balance
        growth = start * exp_ret
        if age < ra:
            contrib = contribution * infl
            withdrawal = 0.0
            ss = 0.0
        else:
            contrib = 0.0
            ss = ss_amt * infl if age >= ss_age else 0.0
            withdrawal = spend * infl
        balance = start + growth + contrib - (withdrawal - ss)
        if balance < 0:
            balance = 0.0
        rows.append({
            "age": age,
            "start_balance": round(start, 2),
            "contribution": round(contrib, 2),
            "growth": round(growth, 2),
            "withdrawal": round(withdrawal, 2),
            "ss": round(ss, 2),
            "end_balance": round(balance, 2),
        })
    return rows


def run_all(assumptions: dict, n_paths: int = 10_000, seed: int | None = None) -> dict:
    """Run the base case plus any what-if scenarios. Each scenario is the base
    assumptions with its overrides applied — the base case is never mutated."""
    base = run_simulation(assumptions, n_paths=n_paths, seed=seed)
    scenarios = []
    for sc in assumptions.get("scenarios", []) or []:
        overrides = {k: v for k, v in sc.items() if k != "name"}
        merged = {**assumptions, **overrides}
        merged.pop("scenarios", None)
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
