import sys, unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import retirement_model as rm


def base_assumptions(**overrides):
    """A reasonable, well-funded set of assumptions for tests to tweak."""
    a = {
        "current_age": 37,
        "retirement_age": 62,
        "plan_through_age": 95,
        "current_balance": 500_000.0,
        "annual_contribution": 40_000.0,
        "retirement_spend": 120_000.0,
        "inflation": 0.03,
        "expected_return": 0.075,
        "volatility": 0.13,
        "ss_start_age": 67,
        "ss_annual_amount": 40_000.0,
    }
    a.update(overrides)
    return a


class TestSuccessProbability(unittest.TestCase):
    def test_overfunded_zero_vol_is_certain_success(self):
        a = base_assumptions(
            current_age=60,
            retirement_age=62,
            plan_through_age=65,
            current_balance=10_000_000.0,
            annual_contribution=0.0,
            retirement_spend=50_000.0,
            inflation=0.0,
            expected_return=0.05,
            volatility=0.0,
            ss_annual_amount=0.0,
        )
        result = rm.run_simulation(a, n_paths=200, seed=1)
        self.assertEqual(result["success_probability"], 100.0)

    def test_underfunded_plan_mostly_fails(self):
        a = base_assumptions(
            current_balance=1_000.0,
            annual_contribution=0.0,
            retirement_spend=200_000.0,
            ss_annual_amount=0.0,
        )
        result = rm.run_simulation(a, n_paths=500, seed=7)
        self.assertLess(result["success_probability"], 50.0)


class TestPercentiles(unittest.TestCase):
    def test_percentiles_are_monotonic_each_year(self):
        result = rm.run_simulation(base_assumptions(), n_paths=1000, seed=3)
        for row in result["percentiles"]:
            self.assertLessEqual(row["p10"], row["p50"])
            self.assertLessEqual(row["p50"], row["p90"])

    def test_percentiles_span_current_through_plan_age(self):
        result = rm.run_simulation(base_assumptions(), n_paths=100, seed=3)
        ages = [r["age"] for r in result["percentiles"]]
        self.assertEqual(ages[0], 37)
        self.assertEqual(ages[-1], 95)


class TestDeterministicProjection(unittest.TestCase):
    def test_matches_hand_computed_cashflow(self):
        # zero volatility, so the median return is the mean (10%).
        # age40: 100k * 1.1 + 10k contribution mid-year * sqrt(1.1)          = 120,488.09
        # age41: 50k spend comes out at the start: (120,488.09 - 50k) * 1.1  =  77,536.90
        a = base_assumptions(
            current_age=40,
            retirement_age=41,
            plan_through_age=42,
            current_balance=100_000.0,
            annual_contribution=10_000.0,
            retirement_spend=50_000.0,
            inflation=0.0,
            expected_return=0.10,
            volatility=0.0,
            ss_start_age=67,
            ss_annual_amount=0.0,
        )
        rows = rm.deterministic_projection(a)
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[0]["end_balance"], 100_000 * 1.1 + 10_000 * 1.1 ** 0.5, places=2)
        self.assertAlmostEqual(rows[1]["end_balance"], (rows[0]["end_balance"] - 50_000) * 1.1, places=2)

    def test_uses_median_not_mean_growth(self):
        a = base_assumptions(current_age=40, retirement_age=60, plan_through_age=41,
                             annual_contribution=0.0, current_balance=100_000.0, inflation=0.0)
        rows = rm.deterministic_projection(a)
        median = (1.075) / (1 + (0.13 / 1.075) ** 2) ** 0.5
        self.assertAlmostEqual(rows[0]["end_balance"], 100_000 * median, places=2)
        self.assertLess(rows[0]["median_return"], 0.075)

    def test_deterministic_matches_simulation_median_without_cashflows(self):
        a = base_assumptions(current_age=40, retirement_age=60, plan_through_age=60,
                             annual_contribution=0.0, current_balance=100_000.0, inflation=0.0)
        res = rm.run_simulation(a, n_paths=20_000, seed=11)
        mc_median = res["percentiles"][-1]["p50"]
        det = res["deterministic"][-1]["end_balance"]
        self.assertAlmostEqual(mc_median / det, 1.0, delta=0.05)


class TestTimingAndDepletion(unittest.TestCase):
    def test_withdrawal_at_start_of_year_depletes_that_year(self):
        a = base_assumptions(current_age=60, retirement_age=60, plan_through_age=63,
                             current_balance=100_000.0, annual_contribution=0.0,
                             retirement_spend=60_000.0, inflation=0.0, expected_return=0.0,
                             volatility=0.0, ss_annual_amount=0.0)
        res = rm.run_simulation(a, n_paths=10, seed=1)
        # 100k -> 40k after age 60's withdrawal; age 61 can't be funded
        self.assertEqual(res["success_probability"], 0.0)
        self.assertEqual(res["depletion"]["median_depletion_age"], 61.0)

    def test_social_security_before_retirement_is_income(self):
        a = base_assumptions(current_age=62, retirement_age=64, plan_through_age=63,
                             current_balance=0.0, annual_contribution=0.0, inflation=0.0,
                             expected_return=0.0, volatility=0.0, ss_start_age=62,
                             ss_annual_amount=20_000.0)
        rows = rm.deterministic_projection(a)
        self.assertAlmostEqual(rows[0]["end_balance"], 20_000.0, places=2)


class TestOutputs(unittest.TestCase):
    def test_real_dollars_and_standard_error(self):
        res = rm.run_simulation(base_assumptions(), n_paths=1000, seed=3)
        k = 95 - 37
        self.assertAlmostEqual(res["ending_balance_real"]["p50"],
                               res["ending_balance"]["p50"] / 1.03 ** k, delta=1.0)
        p = res["success_probability"] / 100
        self.assertAlmostEqual(res["success_se"], round((p * (1 - p) / 1000) ** 0.5 * 100, 2), places=2)


class TestCapitalMarketAssumptions(unittest.TestCase):
    CMA = {
        "default_fee": 0.0,
        "asset_classes": {
            "us_equity": {"geometric": 0.06, "volatility": 0.16},
            "intl_equity": {"geometric": 0.06, "volatility": 0.16},
            "bonds": {"geometric": 0.04, "volatility": 0.05},
            "cash": {"geometric": 0.03, "volatility": 0.0},
        },
        "correlations": {"us_equity|intl_equity": 1.0},
    }

    def _a(self, **kw):
        a = base_assumptions(**kw)
        a.pop("expected_return")
        a.pop("volatility")
        a["allocation"] = {"us_equity": 0.5, "intl_equity": 0.5, "bonds": 0.0, "cash": 0.0}
        a["cma"] = self.CMA
        return a

    def test_all_stock_portfolio_prices_at_stock_assumptions(self):
        sched = rm.return_schedule(self._a())
        self.assertAlmostEqual(sched[0]["mean"], 0.06 + 0.16 ** 2 / 2, places=6)
        self.assertAlmostEqual(sched[0]["vol"], 0.16, places=6)

    def test_fee_is_subtracted(self):
        a = self._a()
        a["fee"] = 0.01
        self.assertAlmostEqual(rm.return_schedule(a)[0]["mean"], 0.06 + 0.16 ** 2 / 2 - 0.01, places=6)

    def test_glide_path_moves_equity_share(self):
        a = self._a(current_age=40, retirement_age=50, plan_through_age=60)
        a["glide_path"] = {"retirement_equity": 0.6, "end_equity": 0.2}
        sched = rm.return_schedule(a)
        self.assertAlmostEqual(sched[0]["equity"], 1.0)
        self.assertAlmostEqual(sched[5]["equity"], 0.8)
        self.assertAlmostEqual(sched[10]["equity"], 0.6)
        self.assertAlmostEqual(sched[15]["equity"], 0.4)
        self.assertLess(sched[15]["vol"], sched[0]["vol"])

    def test_shipped_cma_file_loads(self):
        cma = rm.load_cma()
        self.assertEqual(set(cma["asset_classes"]), set(rm.ASSET_CLASSES))
        self.assertIn("as_of", cma)


class TestValidation(unittest.TestCase):
    def test_rejects_bad_inputs(self):
        self.assertTrue(rm.validate(base_assumptions(ss_start_age=60)))
        self.assertTrue(rm.validate(base_assumptions(current_balance=-1)))
        self.assertTrue(rm.validate(base_assumptions(retirement_age=30)))
        bad = base_assumptions(allocation={"us_equity": 0.5, "bonds": 0.2})
        self.assertTrue(any("sum to 1" in p for p in rm.validate(bad)))
        self.assertEqual(rm.validate(base_assumptions()), [])

    def test_no_ss_amount_skips_age_check(self):
        self.assertEqual(rm.validate(base_assumptions(ss_start_age=100, ss_annual_amount=0)), [])


class TestScenarios(unittest.TestCase):
    def test_scenarios_run_without_mutating_base(self):
        a = base_assumptions(scenarios=[{"name": "Retire at 60", "retirement_age": 60}])
        out = rm.run_all(a, n_paths=100, seed=5)
        self.assertEqual(a["retirement_age"], 62)  # base untouched
        self.assertEqual(len(out["scenarios"]), 1)
        self.assertEqual(out["scenarios"][0]["name"], "Retire at 60")
        self.assertEqual(out["scenarios"][0]["assumptions"]["retirement_age"], 60)


class TestScenarioNullOverride(unittest.TestCase):
    def test_null_override_removes_key(self):
        a = base_assumptions(allocation={"us_equity": 1.0, "intl_equity": 0, "bonds": 0, "cash": 0},
                             scenarios=[{"name": "Scalar", "allocation": None}])
        out = rm.run_all(a, n_paths=50, seed=1)
        self.assertNotIn("allocation", out["scenarios"][0]["assumptions"])
        self.assertIn("allocation", out["base"]["assumptions"])


class TestAllocationMapping(unittest.TestCase):
    def test_known_buckets(self):
        self.assertEqual(rm.returns_for_allocation(80), (0.075, 0.13))
        self.assertEqual(rm.returns_for_allocation(60), (0.060, 0.10))


if __name__ == "__main__":
    unittest.main()
