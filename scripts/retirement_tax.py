"""Tax and Social Security rules for the retirement engine (pure computation, numpy).

Figures come from scripts/retirement_data/tax_<year>.json (refreshed yearly). Every function
takes numpy arrays (one value per simulated path) or scalars.

Indexing convention: bracket and deduction thresholds are treated as inflation-indexed, so
income is deflated to today's dollars before they are applied and the tax is inflated back.
The Social Security taxation thresholds and the NIIT threshold are not indexed in law, so
they are applied to nominal amounts.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

TAX_PATH = Path(__file__).resolve().parent / "retirement_data" / "tax_2026.json"


def load_tax(path: Path | str | None = None) -> dict:
    return json.loads(Path(path or TAX_PATH).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Social Security
# ---------------------------------------------------------------------------


def claim_factor(claim_age: float, fra: float = 67) -> float:
    """Benefit as a share of the full (PIA) benefit for a claim age: 5/9% per month for the
    first 36 months early, 5/12% per month beyond; 8% per year of delay up to 70."""
    if claim_age < fra:
        months = (fra - claim_age) * 12
        return 1.0 - (min(months, 36) * 5 / 9 + max(months - 36, 0) * 5 / 12) / 100
    return 1.0 + 0.08 * (min(claim_age, 70) - fra)


def _pia_from_aime(aime: float, bend: list[float]) -> float:
    b1, b2 = bend
    return 0.9 * min(aime, b1) + 0.32 * max(0.0, min(aime, b2) - b1) + 0.15 * max(0.0, aime - b2)


def _aime_from_pia(pia: float, bend: list[float]) -> float:
    b1, b2 = bend
    t1, t2 = 0.9 * b1, 0.9 * b1 + 0.32 * (b2 - b1)
    if pia <= t1:
        return pia / 0.9
    if pia <= t2:
        return b1 + (pia - t1) / 0.32
    return b2 + (pia - t2) / 0.15


def effective_pia(pia_monthly: float, years_worked: float, tax: dict) -> float:
    """Scale a statement's full benefit for fewer than 35 earning years (statements assume
    work continues to the benefit age). Zero years replace earning years in the 35-year
    average, which is then run back through the bend-point formula."""
    ss = tax["social_security"]
    n = ss["computation_years"]
    if years_worked >= n or pia_monthly <= 0:
        return pia_monthly
    aime = _aime_from_pia(pia_monthly, ss["bend_points"]) * max(years_worked, 0) / n
    return _pia_from_aime(aime, ss["bend_points"])


def taxable_social_security(ss, other_income, thresholds):
    """Federally taxable Social Security (nominal), from provisional income =
    other income + half of benefits. Up to 50% between the thresholds, 85% above."""
    ss = np.asarray(ss, dtype=float)
    lo, hi = thresholds
    prov = np.asarray(other_income, dtype=float) + 0.5 * ss
    tier1 = np.minimum(0.5 * ss, 0.5 * np.clip(prov - lo, 0.0, hi - lo))
    tier2 = 0.85 * np.maximum(prov - hi, 0.0)
    return np.minimum(0.85 * ss, tier1 + tier2)


# ---------------------------------------------------------------------------
# income tax
# ---------------------------------------------------------------------------


def bracket_tax(x, brackets):
    """Progressive tax on x for [[upper, rate], ...] (upper None = no cap)."""
    x = np.asarray(x, dtype=float)
    tax = np.zeros_like(x)
    lower = 0.0
    for upper, rate in brackets:
        top = np.inf if upper is None else float(upper)
        tax += rate * np.clip(x - lower, 0.0, top - lower)
        lower = top
    return tax


def _ltcg_tax(ordinary_taxable, gains, brackets):
    """Capital gains stacked on top of ordinary taxable income."""
    gains = np.asarray(gains, dtype=float)
    return bracket_tax(ordinary_taxable + gains, brackets) - bracket_tax(ordinary_taxable, brackets)


def income_tax(ordinary, gains, ss, deflator, *, status: str = "mfj", seniors: int = 0,
               state: str | None = "MN", tax: dict) -> dict:
    """Federal + state income tax (nominal) for one year.

    ordinary  pre-tax withdrawals and other ordinary income, nominal (excludes Social Security)
    gains     long-term capital gains realized, nominal
    ss        Social Security benefits received, nominal
    deflator  (1 + inflation)**k, to express income in the tax table's dollars
    seniors   people aged 65+ (each adds the senior standard deduction)
    Returns {"federal", "state", "total"} arrays.
    """
    fed = tax["federal"][status]
    ordinary = np.asarray(ordinary, dtype=float)
    gains = np.asarray(gains, dtype=float)
    ss = np.asarray(ss, dtype=float)

    ss_taxable = taxable_social_security(ss, ordinary + gains, fed["ss_thresholds"])
    agi = ordinary + gains + ss_taxable
    deduction = fed["standard_deduction"] + seniors * fed["senior_addition_per_person"]

    ord_real = (ordinary + ss_taxable) / deflator
    gains_real = gains / deflator
    ord_taxable = np.maximum(ord_real - deduction, 0.0)
    # unused deduction shelters gains too
    gains_taxable = np.maximum(gains_real - np.maximum(deduction - ord_real, 0.0), 0.0)
    federal_real = bracket_tax(ord_taxable, fed["brackets"]) + _ltcg_tax(ord_taxable, gains_taxable, fed["ltcg"])
    niit = 0.038 * np.minimum(gains, np.maximum(agi - fed["niit_threshold"], 0.0))
    federal = federal_real * deflator + niit

    state_tax = np.zeros_like(federal)
    if state:
        st = tax["state"][state][status]
        agi_real = agi / deflator
        over = np.maximum(agi_real - st["ss_full_subtraction_agi"], 0.0)
        keep = np.clip(1.0 - np.ceil(over / st["ss_phaseout_step"]) * st["ss_phaseout_pct"], 0.0, 1.0)
        st_taxable = np.maximum(agi_real - keep * ss_taxable / deflator - st["standard_deduction"], 0.0)
        state_tax = bracket_tax(st_taxable, st["brackets"]) * deflator
    return {"federal": federal, "state": state_tax, "total": federal + state_tax}
