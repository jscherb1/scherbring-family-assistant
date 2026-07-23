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
        # age40: start 100k, +10% growth (10k), +10k contrib -> 120k
        # age41: start 120k, +10% growth (12k), -50k spend -> 82k
        a = base_assumptions(
            current_age=40,
            retirement_age=41,
            plan_through_age=42,
            current_balance=100_000.0,
            annual_contribution=10_000.0,
            retirement_spend=50_000.0,
            inflation=0.0,
            expected_return=0.10,
            ss_start_age=100,
            ss_annual_amount=0.0,
        )
        rows = rm.deterministic_projection(a)
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[0]["end_balance"], 120_000.0, places=2)
        self.assertAlmostEqual(rows[1]["end_balance"], 82_000.0, places=2)


class TestScenarios(unittest.TestCase):
    def test_scenarios_run_without_mutating_base(self):
        a = base_assumptions(scenarios=[{"name": "Retire at 60", "retirement_age": 60}])
        out = rm.run_all(a, n_paths=100, seed=5)
        self.assertEqual(a["retirement_age"], 62)  # base untouched
        self.assertEqual(len(out["scenarios"]), 1)
        self.assertEqual(out["scenarios"][0]["name"], "Retire at 60")
        self.assertEqual(out["scenarios"][0]["assumptions"]["retirement_age"], 60)


class TestAllocationMapping(unittest.TestCase):
    def test_known_buckets(self):
        self.assertEqual(rm.returns_for_allocation(80), (0.075, 0.13))
        self.assertEqual(rm.returns_for_allocation(60), (0.060, 0.10))


if __name__ == "__main__":
    unittest.main()
