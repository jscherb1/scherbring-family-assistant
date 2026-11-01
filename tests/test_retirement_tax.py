"""Tax and Social Security rules (scripts/retirement_tax.py)."""
import numpy as np
import pytest

import retirement_tax as rt

TAX = rt.load_tax()


def test_claim_factor_matches_ssa_rules():
    assert rt.claim_factor(67) == pytest.approx(1.0)
    assert rt.claim_factor(62) == pytest.approx(0.70)
    assert rt.claim_factor(70) == pytest.approx(1.24)
    assert rt.claim_factor(64) == pytest.approx(0.80)


def test_effective_pia_only_drops_below_35_years():
    assert rt.effective_pia(4000, 40, TAX) == 4000
    lower = rt.effective_pia(4000, 33, TAX)
    assert 3700 < lower < 4000
    # inverse/forward formula round-trips
    assert rt._pia_from_aime(rt._aime_from_pia(2000, [1286, 7749]), [1286, 7749]) == pytest.approx(2000)


def test_taxable_social_security_tiers():
    assert rt.taxable_social_security(20000, 10000, (32000, 44000)) == 0
    assert rt.taxable_social_security(40000, 200000, (32000, 44000)) == pytest.approx(34000)  # 85% cap


def test_federal_ordinary_tax_hand_computed():
    # taxable income 100,800 after the 32,200 deduction: 10% of 24,800 + 12% of 76,000
    t = rt.income_tax(np.array([133000.0]), np.array([0.0]), np.array([0.0]), 1.0, state=None, tax=TAX)
    assert t["federal"][0] == pytest.approx(2480 + 9120)


def test_gains_inside_zero_bracket_and_unused_deduction():
    t = rt.income_tax(np.array([0.0]), np.array([100000.0]), np.array([0.0]), 1.0, state=None, tax=TAX)
    assert t["federal"][0] == pytest.approx(0.0)


def test_niit_on_gains_above_threshold():
    t = rt.income_tax(np.array([300000.0]), np.array([100000.0]), np.array([0.0]), 1.0, state=None, tax=TAX)
    assert t["federal"][0] > 0
    # 200k ordinary fills the 0% band, so all 50k of gains are at 15%; AGI 250k is not over the NIIT line
    no_niit = rt.income_tax(np.array([200000.0]), np.array([50000.0]), np.array([0.0]), 1.0, state=None, tax=TAX)
    gains_tax = no_niit["federal"][0] - rt.income_tax(np.array([200000.0]), np.array([0.0]), np.array([0.0]), 1.0,
                                                       state=None, tax=TAX)["federal"][0]
    assert gains_tax == pytest.approx(0.15 * 50000, rel=0.01)
    # partly in the 0% band: 100k ordinary leaves 31,100 of the 98,900 band
    part = rt.income_tax(np.array([100000.0]), np.array([50000.0]), np.array([0.0]), 1.0, state=None, tax=TAX)
    base = rt.income_tax(np.array([100000.0]), np.array([0.0]), np.array([0.0]), 1.0, state=None, tax=TAX)
    assert part["federal"][0] - base["federal"][0] == pytest.approx(0.15 * (50000 - 31100), rel=0.01)


def test_brackets_are_indexed_by_the_deflator():
    one = rt.income_tax(np.array([150000.0]), np.array([0.0]), np.array([0.0]), 1.0, tax=TAX)["total"][0]
    two = rt.income_tax(np.array([300000.0]), np.array([0.0]), np.array([0.0]), 2.0, tax=TAX)["total"][0]
    assert two == pytest.approx(2 * one)


def test_minnesota_exempts_social_security_for_moderate_income():
    with_ss = rt.income_tax(np.array([60000.0]), np.array([0.0]), np.array([40000.0]), 1.0, tax=TAX)
    fed_only = rt.income_tax(np.array([60000.0]), np.array([0.0]), np.array([40000.0]), 1.0, state=None, tax=TAX)
    no_ss = rt.income_tax(np.array([60000.0]), np.array([0.0]), np.array([0.0]), 1.0, tax=TAX)
    assert with_ss["state"][0] == pytest.approx(no_ss["state"][0])
    assert fed_only["state"][0] == 0
