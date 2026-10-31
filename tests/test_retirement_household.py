"""Household mode of the engine: tax buckets, taxes, RMDs, two-person Social Security,
cash-flow events, the kids' bucket and the survivor case."""
import pytest

import retirement_model as rm


def household(**kw):
    a = {
        "current_age": 60, "retirement_age": 62, "plan_through_age": 80, "retirement_spend": 80_000,
        "inflation": 0.0, "expected_return": 0.0, "volatility": 0.0,
        "buckets": {"taxable": 500_000, "pretax": 1_000_000, "roth": 100_000},
        "contributions": {"pretax": 20_000, "taxable": 10_000},
        "people": [{"name": "A", "age_offset": 0, "ss_pia": 40_000, "ss_claim_age": 67},
                   {"name": "B", "age_offset": 0, "ss_pia": 30_000, "ss_claim_age": 67}],
        "tax": {"enabled": True, "state": "MN"}, "taxable_gain_fraction": 0.5,
    }
    a.update(kw)
    return a


def rows_by_age(a):
    return {r["age"]: r for r in rm.deterministic_projection(a)}


def test_single_pool_household_equals_legacy_model():
    legacy = {"current_age": 40, "retirement_age": 50, "plan_through_age": 70, "current_balance": 300_000,
              "annual_contribution": 20_000, "retirement_spend": 40_000, "inflation": 0.02, "expected_return": 0.05,
              "volatility": 0.1, "ss_start_age": 67, "ss_annual_amount": 15_000}
    hh = {**legacy, "buckets": {"taxable": 300_000}, "contributions": {"taxable": 20_000},
          "people": [{"name": "x", "ss_benefit": 15_000, "ss_claim_age": 67}]}
    a, b = rm.deterministic_projection(legacy), rm.deterministic_projection(hh)
    assert [r["end_balance"] for r in a] == [r["end_balance"] for r in b]
    assert rm.run_simulation(legacy, 500, 3)["success_probability"] == rm.run_simulation(hh, 500, 3)["success_probability"]


def test_taxes_reduce_balance_and_are_reported():
    taxed = rows_by_age(household())
    untaxed = rows_by_age(household(tax=None))
    assert taxed[79]["end_balance"] < untaxed[79]["end_balance"]
    assert sum(r["taxes"] for r in taxed.values()) > 0
    assert all(r["taxes"] == 0 for r in untaxed.values())


def test_spending_comes_from_taxable_first_then_pretax():
    r = rows_by_age(household())
    assert r[62]["buckets"]["taxable"] < 500_000 + 10_000 * 2
    assert r[62]["buckets"]["pretax"] == pytest.approx(1_000_000 + 40_000)  # untouched, plus two years of saving


def test_rmds_force_pretax_withdrawals_at_75():
    a = household(buckets={"taxable": 5_000_000, "pretax": 1_000_000, "roth": 0}, contributions={})
    r = rows_by_age(a)
    assert r[74]["buckets"]["pretax"] == pytest.approx(1_000_000)
    assert r[75]["buckets"]["pretax"] == pytest.approx(1_000_000 - 1_000_000 / 24.6, rel=1e-6)


def test_early_pretax_withdrawals_pay_the_penalty():
    a = household(current_age=55, retirement_age=55, buckets={"taxable": 0, "pretax": 2_000_000, "roth": 0},
                  contributions={})
    r = rows_by_age(a)
    # age 55 pays 10% penalty on the pretax draw; age 60 does not
    assert r[55]["taxes"] > r[60]["taxes"]


def test_social_security_claim_ages_and_survivor():
    a = household(people=[{"name": "A", "ss_pia": 40_000, "ss_claim_age": 62},
                          {"name": "B", "ss_pia": 30_000, "ss_claim_age": 70}])
    r = rows_by_age(a)
    assert r[62]["ss"] == pytest.approx(40_000 * 0.70)
    assert r[70]["ss"] == pytest.approx(40_000 * 0.70 + 30_000 * 1.24)
    s = rows_by_age({**a, "survivor": {"first_death_age": 75, "spend_factor": 0.75}})
    assert s[75]["ss"] == pytest.approx(30_000 * 1.24)  # the larger benefit continues
    assert s[75]["other_outflows"] == pytest.approx(-0.25 * 80_000)


def test_fewer_than_35_working_years_lowers_the_benefit():
    early = rows_by_age(household(current_age=50, retirement_age=50,
                                  people=[{"name": "A", "ss_pia": 40_000, "work_start_age": 22}]))
    full = rows_by_age(household(people=[{"name": "A", "ss_pia": 40_000, "work_start_age": 22}]))
    assert early[67]["ss"] < full[67]["ss"]


def test_events_mortgage_only_in_retirement_and_min_age():
    events = [{"name": "Mortgage", "start_age": "retirement", "end_age": 63, "amount": 20_000, "real": False},
              {"name": "Medicare", "start_age": "retirement", "min_age": 65, "end_age": "plan_end", "amount": 10_000}]
    r = rows_by_age(household(events=events))
    assert r[61]["other_outflows"] == 0
    assert r[62]["other_outflows"] == pytest.approx(20_000)
    assert r[64]["other_outflows"] == 0
    assert r[65]["other_outflows"] == pytest.approx(10_000)


def test_college_draws_kids_bucket_first_then_household():
    events = [{"name": "College", "start_age": 60, "end_age": 61, "amount": 50_000, "funding": "kids"}]
    r = rows_by_age(household(events=events, kids_balance=70_000, tax=None))
    assert r[60]["other_outflows"] == 0 and r[60]["kids_balance"] == pytest.approx(20_000)
    assert r[61]["other_outflows"] == pytest.approx(30_000)


def test_validation_of_household_inputs():
    assert rm.validate(household()) == []
    assert rm.validate(household(withdrawal_order=["taxable", "taxable", "roth"]))
    assert rm.validate(household(people=[{"name": "A", "ss_pia": 1, "ss_claim_age": 61}]))
    assert rm.validate(household(buckets={"stocks": 1}))
