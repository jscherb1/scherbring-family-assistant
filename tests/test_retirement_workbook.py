import os, sys, tempfile, unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import retirement_model as rm
import retirement_workbook as rw
from openpyxl import load_workbook


def sample_payload():
    assumptions = {
        "current_age": 37, "retirement_age": 62, "plan_through_age": 95,
        "current_balance": 500000.0, "annual_contribution": 40000.0,
        "retirement_spend": 120000.0, "inflation": 0.03,
        "expected_return": 0.075, "volatility": 0.13,
        "ss_start_age": 67, "ss_annual_amount": 40000.0,
        "scenarios": [{"name": "Retire at 60", "retirement_age": 60}],
    }
    result = rm.run_all(assumptions, n_paths=200, seed=1)
    return {
        "generated_date": "2026-07-23",
        "assumptions": assumptions,
        "assumption_sources": {
            "current_balance": "Monarch get_accounts (2026-07-23)",
            "annual_contribution": "Monarch get_cashflow trailing 12mo",
            "expected_return": "Derived from Monarch holdings (80/20)",
        },
        "result": result,
        "accounts": [{"name": "Rollover IRA", "bucket": "pretax", "balance": 500000.0,
                      "allocation": {"us_equity": 0.6, "intl_equity": 0.3, "bonds": 0.1, "cash": 0.0},
                      "allocation_source": "holdings"}],
        "narrative": {
            "headline": "80% chance of funding retirement to 95.",
            "body": "At the current savings rate the plan succeeds in 4 of 5 simulated futures.",
        },
        "data_sources": [
            {"item": "Current balance", "value": "$500,000",
             "source": "Monarch get_accounts", "pulled": "2026-07-23", "overridden": "No"},
        ],
    }


class TestWorkbook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        self.tmp.close()
        self.path = self.tmp.name
        rw.build_workbook(sample_payload(), self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_expected_sheets_present(self):
        wb = load_workbook(self.path)
        for name in ["Summary", "Assumptions", "Base Case", "Monte Carlo", "Data Sources"]:
            self.assertIn(name, wb.sheetnames)
        # scenario sheet exists (name may be truncated/sanitized)
        self.assertTrue(any("Retire at 60" in s for s in wb.sheetnames))

    def test_base_case_uses_live_formulas_referencing_assumptions(self):
        wb = load_workbook(self.path)  # formulas kept as strings, not computed
        ws = wb["Base Case"]
        formula_cells = [
            c.value for row in ws.iter_rows() for c in row
            if isinstance(c.value, str) and c.value.startswith("=")
        ]
        self.assertTrue(formula_cells, "Base Case should contain live formulas")
        self.assertTrue(
            any("Assumptions" in f for f in formula_cells),
            "Base Case formulas should reference the Assumptions sheet",
        )

    def test_assumptions_sheet_has_editable_values(self):
        wb = load_workbook(self.path)
        ws = wb["Assumptions"]
        values = [c.value for row in ws.iter_rows() for c in row]
        self.assertIn(62, values)      # retirement age
        self.assertIn(500000.0, values)  # current balance

    def test_accounts_sheet_and_real_dollar_columns(self):
        wb = load_workbook(self.path)
        self.assertIn("Accounts", wb.sheetnames)
        headers = [c.value for c in wb["Base Case"][4]]
        self.assertIn("Median Return", headers)
        self.assertIn("End (today's $)", headers)


def _evaluate(wb, sheet, ref, cache=None):
    """Tiny evaluator for the Base Case formula subset (IF, MAX, SQRT, ^, cell refs)."""
    import math
    import re
    cache = {} if cache is None else cache
    key = (sheet, ref.replace("$", ""))
    if key in cache:
        return cache[key]
    val = wb[sheet][key[1]].value
    if isinstance(val, str) and val.startswith("="):
        expr = val[1:]

        def sub(m):
            sh = m.group(1) or sheet
            return repr(_evaluate(wb, sh.strip("'"), m.group(2), cache))
        expr = re.sub(r"(?:('[^']+')!)?(\$?[A-Z]+\$?[0-9]+)", sub, expr)
        expr = expr.replace("^", "**").replace("IF(", "_if(").replace("MAX(", "max(").replace("SQRT(", "math.sqrt(")
        expr = re.sub(r"(?<![<>=!])=(?!=)", "==", expr)
        val = eval(expr, {"_if": lambda c, a, b: a if c else b, "math": math, "max": max})
    cache[key] = val
    return val


class FormulaParityTests(unittest.TestCase):
    def test_base_case_formulas_match_engine(self):
        payload = sample_payload()
        payload["assumptions"]["allocation"] = {"us_equity": 0.55, "intl_equity": 0.2, "bonds": 0.2, "cash": 0.05}
        payload["assumptions"]["glide_path"] = {"retirement_equity": 0.6, "end_equity": 0.4}
        payload["assumptions"].pop("expected_return")
        payload["assumptions"].pop("volatility")
        payload["result"] = rm.run_all(payload["assumptions"], n_paths=100, seed=1)
        with tempfile.TemporaryDirectory() as d:
            out = str(Path(d) / "wb.xlsx")
            rw.build_workbook(payload, out)
            wb = load_workbook(out)
            det = payload["result"]["base"]["deterministic"]
            cache = {}
            for i, row in enumerate(det):
                r = 5 + i  # header on row 4 (1-based), data from row 5
                self.assertAlmostEqual(_evaluate(wb, "Base Case", f"I{r}", cache), row["end_balance"], delta=1.0)
                self.assertAlmostEqual(_evaluate(wb, "Base Case", f"J{r}", cache), row["end_balance_real"], delta=1.0)


if __name__ == "__main__":
    unittest.main()


class ResultFileTests(unittest.TestCase):
    def test_cli_reads_result_from_result_file(self):
        import json
        payload = sample_payload()
        with tempfile.TemporaryDirectory() as d:
            rf = Path(d) / "result.json"
            rf.write_text(json.dumps(payload.pop("result")))
            payload["result_file"] = str(rf)
            pf = Path(d) / "payload.json"
            pf.write_text(json.dumps(payload))
            out = Path(d) / "out.xlsx"
            self.assertEqual(rw._main(["--data-file", str(pf), "--out", str(out)]), 0)
            self.assertTrue(out.exists())
