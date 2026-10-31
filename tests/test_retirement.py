"""Shared retirement service (scripts/retirement.py) and its CLI (scripts/retirement_store.py)."""
import json
from datetime import date

import pytest

import money
import retirement
import retirement_store

TODAY = date(2026, 10, 31)

STORED = {
    "current_age": 37, "retirement_age": 62, "plan_through_age": 95, "retirement_spend": 120000,
    "inflation": 0.03, "ss_start_age": 67, "ss_annual_amount": 40000, "fallback_stock_pct": 80,
    "current_balance": None, "annual_contribution": None, "expected_return": None, "volatility": None,
    "scenarios": [{"name": "Retire at 60", "retirement_age": 60}],
}


def _acct(id_, name, type_, subtype, balance, hidden=False):
    return {"id": id_, "name": name, "type": type_, "subtype": subtype, "balance": balance, "hidden": hidden}


def _hold(acct, hid, ticker, name, st, value):
    return {"account_id": acct, "holding_id": hid, "ticker": ticker, "name": name, "security_type": st,
            "value": value}


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(retirement, "DB_PATH", path)
    monkeypatch.setattr(money, "DB_PATH", path)
    monkeypatch.setattr(retirement, "RUN_DIR", tmp_path / "runs")
    run_id = money.start_sync("full", db_path=path)
    money.apply_snapshot({
        "accounts": [
            _acct("ira", "Rollover IRA", "brokerage", "ira", 400_000),
            _acct("roth", "ROTH IRA", "brokerage", "roth", 50_000),
            _acct("tax", "Joint Investment", "brokerage", "brokerage", 50_000),
            _acct("k401", "Old 401a", "brokerage", "st_401a", 100_000),
            _acct("529", "Ruth 529", "brokerage", "education_savings_account", 20_000),
            _acct("car", "Claire Car Fund", "brokerage", "brokerage", 5_000),
            _acct("chk", "Checking", "depository", "checking", 30_000),
            _acct("home", "House", "real_estate", "primary_home", 600_000),
            _acct("old", "Closed 401k", "brokerage", "st_401k", 9_999, hidden=True),
        ],
        "holdings": {"accounts": ["ira", "roth", "tax"], "items": [
            _hold("ira", "h1", "FXAIX", "Fidelity 500 Index Fund", "mutual_fund", 300_000),
            _hold("ira", "h2", "FNBGX", "Fidelity LT Treasury", "mutual_fund", 100_000),
            _hold("roth", "h3", "SPAXX", "Fidelity Government Money Market", "cash", 50_000),
            _hold("tax", "h4", "ZZZZ", "Mystery International Fund", "mutual_fund", 50_000),
        ]},
        "transactions": {"start": "2025-10-01", "end": "2026-09-30", "items": [
            {"id": "t1", "date": "2026-01-15", "amount": 200_000, "group_type": "income"},
            {"id": "t2", "date": "2026-02-15", "amount": -150_000, "group_type": "expense"},
        ]},
    }, db_path=path)
    money.finish_sync(run_id, "ok", db_path=path)
    retirement._set_json(retirement.ASSUMPTIONS_KEY, STORED, path)
    return path


def test_accounts_are_bucketed_by_subtype_type_and_hidden_flag(db):
    by_id = {a["id"]: a for a in retirement.classify_accounts(db)}
    assert by_id["ira"]["bucket"] == "pretax"
    assert by_id["k401"]["bucket"] == "pretax"
    assert by_id["roth"]["bucket"] == "roth"
    assert by_id["tax"]["bucket"] == "taxable"
    assert by_id["529"]["bucket"] == "kids"
    assert by_id["chk"]["bucket"] == "cash"
    assert by_id["home"]["bucket"] == "exclude"
    assert by_id["old"]["bucket"] == "exclude"


def test_override_moves_account_to_kids_and_out_of_retirement_balance(db):
    before = retirement.derive_inputs(db, TODAY)["values"]["current_balance"]
    row = retirement.set_account("Car Fund", bucket="kids", note="Claire's car", db_path=db)
    assert row["bucket"] == "kids" and row["source"] == "override"
    after = retirement.derive_inputs(db, TODAY)
    assert after["values"]["current_balance"] == before - 5_000
    assert after["buckets"]["kids"] == 25_000


def test_derive_inputs_balance_allocation_savings_and_warnings(db):
    d = retirement.derive_inputs(db, TODAY)
    v = d["values"]
    assert v["current_balance"] == 605_000  # ira + roth + tax + 401a + car fund (taxable until reclassified)
    assert v["annual_contribution"] == 50_000
    alloc = v["allocation"]
    assert abs(sum(alloc.values()) - 1) < 1e-3
    # cash: the whole Roth (50k) + Car Fund has no holdings -> fallback, no cash
    assert alloc["cash"] == pytest.approx(50_000 / 605_000, abs=1e-3)
    text = " | ".join(d["warnings"])
    assert "Old 401a" in text and "no holdings" in text
    assert "ZZZZ" in text  # classified by name only
    assert "in cash or money market" in text


def test_security_classification_rules():
    assert retirement.classify_security("FXAIX", "", "mutual_fund")[1] == "known"
    w, src = retirement.classify_security("FDEEX", "Fidelity Freedom 2055", "mutual_fund", today=TODAY)
    assert src.startswith("known target-date") and w["us_equity"] + w["intl_equity"] == pytest.approx(0.9)
    assert retirement.classify_security("X", "Some Bond Index", "mutual_fund")[0]["bonds"] == 1.0
    assert retirement.classify_security("X", "Emerging Mkts", "etf")[0]["intl_equity"] == 1.0
    assert retirement.classify_security("BTC-USD", "Bitcoin", "cryptocurrency")[1].startswith("crypto")
    assert retirement.classify_security("X", "Fund", "etf", overrides={"X": retirement.BONDS})[1] == "override"


def test_allocation_override_accepts_percentages(db):
    row = retirement.set_account("Old 401a", allocation={"us_equity": 60, "intl_equity": 30, "bonds": 10}, db_path=db)
    assert row["allocation_override"]["us_equity"] == pytest.approx(0.6)
    d = retirement.derive_inputs(db, TODAY)
    assert not any("Old 401a" in w for w in d["warnings"])
    with pytest.raises(retirement.RetirementError):
        retirement.set_account("Old 401a", allocation={"stocks": 1.0}, db_path=db)


def test_resolve_adds_idle_cash_scenario_and_default_glide(db):
    r = retirement.resolve_assumptions(db, TODAY)
    a = r["assumptions"]
    assert a["glide_path"] == retirement.DEFAULT_GLIDE
    assert "expected_return" not in a
    names = [s["name"] for s in a["scenarios"]]
    assert "Retire at 60" in names and "Idle cash invested" in names
    idle = next(s for s in a["scenarios"] if s["name"] == "Idle cash invested")
    assert idle["allocation"]["cash"] == 0.0
    assert r["sources"]["current_balance"].startswith("Monarch snapshot")


def test_contribution_override_and_run_overrides_win(db):
    retirement.set_assumption("contribution_override", 70_000, db_path=db)
    r = retirement.resolve_assumptions(db, TODAY, overrides={"retirement_age": 60})
    assert r["assumptions"]["annual_contribution"] == 70_000
    assert r["assumptions"]["retirement_age"] == 60
    assert r["sources"]["retirement_age"] == "user override (this run)"


def test_set_assumption_validates(db):
    with pytest.raises(retirement.RetirementError):
        retirement.set_assumption("ss_start_age", 55, db_path=db)
    with pytest.raises(retirement.RetirementError):
        retirement.set_assumption("glide_path", {"retirement_equity": 2}, db_path=db)
    assert retirement.set_assumption("retirement_spend", 110_000, db_path=db)["retirement_spend"] == 110_000


def test_run_writes_files_and_workbook_payload(db, tmp_path):
    out = retirement.run(db, TODAY, n_paths=200, seed=1)
    assert 0 <= out["base"]["success_probability"] <= 100
    assert any(s["name"] == "Idle cash invested" for s in out["scenarios"])
    payload = retirement.workbook_payload(TODAY.isoformat(), "Headline", "Body")
    assert payload["result"]["base"]["success_probability"] == out["base"]["success_probability"]
    assert payload["accounts"] and payload["narrative"]["headline"] == "Headline"
    import retirement_workbook
    path = retirement_workbook.build_workbook(payload, str(tmp_path / "wb.xlsx"))
    assert (tmp_path / "wb.xlsx").exists() and path


def test_missing_snapshot_is_not_found(tmp_path):
    path = tmp_path / "empty.db"
    retirement._set_json(retirement.ASSUMPTIONS_KEY, STORED, path)
    with pytest.raises(retirement.RetirementError) as e:
        retirement.derive_inputs(path, TODAY)
    assert e.value.code == 1


# --- CLI -------------------------------------------------------------------


def test_cli_inputs_and_exit_codes(db, capsys):
    assert retirement_store.main(["inputs"]) == 0
    assert json.loads(capsys.readouterr().out)["values"]["current_balance"] == 605_000
    assert retirement_store.main(["accounts", "classify", "--account", "nope", "--bucket", "kids"]) == 1
    assert "no account matches" in capsys.readouterr().err
    assert retirement_store.main(["config", "set", "--key", "retirement_spend", "--value", "{bad"]) == 2
    assert retirement_store.main(["config", "set", "--key", "ss_start_age", "--value", "55"]) == 2


def test_cli_run_and_workbook(db, capsys, tmp_path):
    assert retirement_store.main(["run", "--date", "2026-10-31", "--paths", "200",
                                  "--scenarios", '[{"name": "Spend 100k", "retirement_spend": 100000}]']) == 0
    out = json.loads(capsys.readouterr().out)
    assert any(s["name"] == "Spend 100k" for s in out["scenarios"])
    xlsx = tmp_path / "out.xlsx"
    assert retirement_store.main(["workbook", "--date", "2026-10-31", "--headline", "H", "--body", "B",
                                  "--out", str(xlsx)]) == 0
    assert xlsx.exists()
    assert retirement_store.main(["workbook", "--date", "2020-01-01", "--headline", "H", "--body", "B",
                                  "--out", str(tmp_path / "missing.xlsx")]) == 1


def test_rerun_same_day_never_overwrites(db, tmp_path, capsys):
    first = retirement.run(db, TODAY, n_paths=50, seed=1)
    second = retirement.run(db, TODAY, n_paths=50, seed=1)
    third = retirement.run(db, TODAY, n_paths=50, seed=1, tag="whatif")
    fourth = retirement.run(db, TODAY, n_paths=50, seed=1, tag="whatif")
    assert [first["tag"], second["tag"], third["tag"], fourth["tag"]] == ["", "2", "whatif", "whatif-2"]
    assert len({r["files"]["run"] for r in (first, second, third, fourth)}) == 4
    xlsx = tmp_path / "wb.xlsx"
    args = ["workbook", "--date", TODAY.isoformat(), "--headline", "H", "--body", "B", "--out", str(xlsx)]
    assert retirement_store.main(args) == 0
    assert retirement_store.main(args) == 2
    assert "already exists" in capsys.readouterr().err


HOUSEHOLD = {
    "people": [
        {"name": "Alex", "birth_date": "1989-06-15", "gross_salary": 300000, "deferral_pct": 0.10,
         "employer_contribution": 1000, "ss_pia_monthly": 4000, "ss_claim_age": 67},
        {"name": "Sam", "birth_date": "1989-02-10", "gross_salary": 100000, "deferral_pct": 0.10,
         "ss_pia_monthly": 3000, "ss_claim_age": 67},
    ],
    "mortgage": {"monthly_pi": 1000, "payoff": "2050-11"},
    "college": {"per_kid_today": 100000, "years": 4, "start_age": 18,
                "kids": [{"name": "Kid", "birth_date": "2019-04-10"}]},
    "kids_contribution": 1000, "emergency_reserve_months": 6,
    "healthcare": {"pre_medicare_annual": 20000, "medicare_annual": 10000, "real_growth": 0.0},
}


@pytest.fixture
def hdb(db):
    stored = {k: v for k, v in STORED.items() if not k.startswith("ss_")}
    stored["household"] = HOUSEHOLD
    stored["scenarios"] = [{"name": "Retire at 55", "retirement_age": 55},
                           {"name": "College 200k", "college_per_kid": 200000},
                           {"name": "Reserve 12 months", "reserve_months": 12},
                           {"name": "Claim at 70", "ss_claim_age": 70},
                           {"name": "Sam lower", "people_overrides": {"Sam": {"ss_pia_monthly": 2000}}},
                           {"name": "Survivor", "first_death_age": 85}]
    retirement._set_json(retirement.ASSUMPTIONS_KEY, stored, db)
    return db


def test_household_build_contributions_reserve_and_events(hdb):
    r = retirement.resolve_assumptions(hdb, TODAY)
    a = r["assumptions"]
    # deferrals capped at the IRS limit for Justin (30k -> 24.5k) + 10k + 1k employer
    assert a["contributions"]["pretax"] == pytest.approx(24_500 + 10_000 + 1_000)
    assert a["contributions"]["taxable"] == 50_000
    # 6 months of 150k/yr spending held back from 30k cash -> nothing excess
    assert r["household"]["reserve"] == pytest.approx(75_000)
    assert a["buckets"]["taxable"] == pytest.approx(55_000)  # tax 50k + car fund 5k, no excess cash
    names = [e["name"] for e in a["events"]]
    assert "College: Kid" in names and "Mortgage P&I (fixed, through payoff)" in names
    college = next(e for e in a["events"] if e["name"] == "College: Kid")
    assert college["start_age"] == 48 and college["amount"] == 25_000 and college["funding"] == "kids"
    assert a["current_age"] == 37 and a["people"][1]["age_offset"] == 0


def test_household_scenarios_are_fully_built(hdb):
    a = retirement.resolve_assumptions(hdb, TODAY)["assumptions"]
    sc = {s["name"]: s for s in a["scenarios"]}
    assert sc["Retire at 55"]["retirement_age"] == 55
    assert next(e for e in sc["College 200k"]["events"] if e["name"].startswith("College"))["amount"] == 50_000
    assert all(p["ss_claim_age"] == 70 for p in sc["Claim at 70"]["people"])
    assert sc["Sam lower"]["people"][1]["ss_pia"] == 24_000
    assert sc["Survivor"]["survivor"]["first_death_age"] == 85
    assert "household" not in sc["Survivor"]


def test_household_run_and_workbook(hdb, tmp_path):
    out = retirement.run(hdb, TODAY, n_paths=200, seed=1)
    assert {s["name"] for s in out["scenarios"]} >= {"Retire at 55", "Survivor"}
    payload = retirement.workbook_payload(TODAY.isoformat(), "H", "B")
    import retirement_workbook
    retirement_workbook.build_workbook(payload, str(tmp_path / "h.xlsx"))
    assert (tmp_path / "h.xlsx").exists()


def test_set_assumption_rejects_bad_household_and_keeps_old(hdb):
    before = retirement.get_assumptions(hdb)
    bad = dict(HOUSEHOLD, people=[dict(HOUSEHOLD["people"][0], ss_claim_age=75)])
    with pytest.raises(retirement.RetirementError):
        retirement.set_assumption("household", bad, db_path=hdb, today=TODAY)
    assert retirement.get_assumptions(hdb) == before


def test_scenario_null_override_really_removes_base_allocation(hdb):
    retirement.set_assumption("scenarios", [{"name": "Scalar", "allocation": None, "glide_path": None,
                                             "expected_return": 0.02, "volatility": 0.0}], db_path=hdb, today=TODAY)
    out = retirement.run(hdb, TODAY, n_paths=200, seed=1)
    scalar = next(s for s in out["scenarios"] if s["name"] == "Scalar")
    assert scalar["success_probability"] != out["base"]["success_probability"] or scalar["delta_pp"] != 0
    _, a, result = retirement.load_run(TODAY.isoformat(), out["tag"])
    sc = next(s for s in result["scenarios"] if s["name"] == "Scalar")
    assert "allocation" not in sc["assumptions"] and sc["returns"]["now"]["mean"] == 0.02


def test_extra_events_scenario_lowers_success(hdb):
    out = retirement.run(hdb, TODAY, n_paths=300, seed=2, extra_scenarios=[
        {"name": "Big purchase", "extra_events": [{"name": "Lake place", "start_age": 40, "end_age": 40,
                                                    "amount": 600000}]}])
    big = next(s for s in out["scenarios"] if s["name"] == "Big purchase")
    assert big["ending_balance_real_p50"] < out["base"]["ending_balance_real_p50"]
