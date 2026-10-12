import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import chores  # noqa: E402
import chores_store  # noqa: E402

MON = date(2026, 10, 12)  # a Monday
SUN = MON + timedelta(days=6)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "t.db"


@pytest.fixture
def no_mystery(db):
    chores.set_config("mystery_enabled", "0", db_path=db)


def kid(db, name="Ruth"):
    return chores.add_kid(name, db_path=db)["id"]


def chore(db, kid_id, name="Brush teeth", freq="daily", value=50, **kw):
    c = chores.create_chore(name, freq, value_cents=value, db_path=db, **kw)
    chores.assign(c["id"], kid_id, db_path=db)
    return c["id"]


def do(db, kid_id, chore_id, day, **kw):
    return chores.complete(kid_id, chore_id, log_date=day.isoformat(), db_path=db, today=day, **kw)


# ---- setup / validation ----

def test_add_kid_defaults_and_unique_name(db):
    k = chores.add_kid("Ruth", db_path=db)
    assert k["avatar"] and k["color"]
    with pytest.raises(chores.ChoreError):
        chores.add_kid("ruth", db_path=db)


def test_resolve_kid_by_fragment_and_not_found(db):
    kid(db, "Claire")
    assert chores.resolve_kid("clai", db)["name"] == "Claire"
    with pytest.raises(chores.ChoreError) as e:
        chores.resolve_kid("zzz", db)
    assert e.value.code == 1


def test_create_chore_validation(db):
    with pytest.raises(chores.ChoreError):
        chores.create_chore("", "daily", db_path=db)
    with pytest.raises(chores.ChoreError):
        chores.create_chore("x", "monthly", db_path=db)
    with pytest.raises(chores.ChoreError):
        chores.create_chore("x", "daily", days="mon", db_path=db)  # days are weekly-only
    with pytest.raises(chores.ChoreError):
        chores.create_chore("x", "weekly", 3, days="mon,tue", db_path=db)  # target != len(days)
    with pytest.raises(chores.ChoreError):
        chores.create_chore("x", "weekly", 8, db_path=db)
    with pytest.raises(chores.ChoreError):
        chores.create_chore("x", "weekly", 1, days="funday", db_path=db)
    c = chores.create_chore("Trash", "weekly", days="Thu, mon", db_path=db)
    assert c["days"] == ["mon", "thu"] and c["target_count"] == 2


def test_update_chore_frequency_drops_days(db):
    c = chores.create_chore("Trash", "weekly", days="mon,thu", db_path=db)
    u = chores.update_chore(c["id"], frequency="daily", db_path=db)
    assert u["days"] == [] and u["target_count"] == 1
    u = chores.update_chore(c["id"], frequency="weekly", target_count=3, db_path=db)
    assert u["target_count"] == 3


def test_money_helpers():
    assert chores.parse_money("$1.50") == 150
    assert chores.parse_money(2) == 200
    assert chores.money(5) == "$0.05" and chores.money(-250) == "-$2.50"
    with pytest.raises(chores.ChoreError):
        chores.parse_money("-1")
    with pytest.raises(chores.ChoreError):
        chores.parse_money("abc")


# ---- completion, money, caps ----

def test_complete_earns_and_streak_reuses_habits(db, no_mystery):
    k = kid(db)
    c = chore(db, k)
    do(db, k, c, MON)
    r = do(db, k, c, MON + timedelta(days=1))
    assert r["earned_cents"] == 50 and r["balance_cents"] == 100
    assert r["status"]["current_streak"] == 2
    assert chores.balance(k, db_path=db) == 100


def test_daily_chore_cannot_be_done_twice_a_day(db):
    k = kid(db)
    c = chore(db, k)
    do(db, k, c, MON)
    with pytest.raises(chores.ChoreError):
        do(db, k, c, MON)
    assert chores.balance(k, db_path=db) == 50


def test_weekly_chore_once_a_day_and_capped_per_week(db):
    k = kid(db)
    c = chore(db, k, "Room", "weekly", target_count=2)
    do(db, k, c, MON)
    with pytest.raises(chores.ChoreError):
        do(db, k, c, MON)  # already today
    do(db, k, c, MON + timedelta(days=1))
    with pytest.raises(chores.ChoreError):
        do(db, k, c, MON + timedelta(days=2))  # weekly target met


def test_future_date_and_unassigned_and_paused_rejected(db):
    k, k2 = kid(db), kid(db, "Claire")
    c = chore(db, k)
    with pytest.raises(chores.ChoreError):
        chores.complete(k, c, log_date=(MON + timedelta(days=1)).isoformat(), db_path=db, today=MON)
    with pytest.raises(chores.ChoreError):
        chores.complete(k2, c, db_path=db, today=MON)  # not assigned
    chores.update_chore(c, active=False, db_path=db)
    with pytest.raises(chores.ChoreError):
        chores.complete(k, c, db_path=db, today=MON)


def test_shared_chore_earns_and_streaks_separately(db, no_mystery):
    a, b = kid(db), kid(db, "Claire")
    c = chore(db, a)
    chores.assign(c, b, db_path=db)
    do(db, a, c, MON)
    assert chores.balance(b, db_path=db) == 0
    assert chores.kid_view(b, db_path=db, today=MON)["items"][0]["current_streak"] == 0


def test_undo_removes_completion_and_money_today_only(db, no_mystery):
    k = kid(db)
    c = chore(db, k)
    do(db, k, c, MON)
    u = chores.undo(k, c, db_path=db, today=MON)
    assert u["removed_cents"] == 50 and chores.balance(k, db_path=db) == 0
    with pytest.raises(chores.ChoreError) as e:
        chores.undo(k, c, db_path=db, today=MON)
    assert e.value.code == 1
    do(db, k, c, MON)
    with pytest.raises(chores.ChoreError):  # a different day: kid path can't touch it
        chores.undo(k, c, db_path=db, today=MON + timedelta(days=1))
    chores.undo(k, c, any_date=True, db_path=db, today=MON + timedelta(days=1))


def test_undo_refused_after_payout(db, no_mystery):
    k = kid(db)
    c = chore(db, k)
    do(db, k, c, MON)
    chores.payout(k, 50, db_path=db)
    with pytest.raises(chores.ChoreError):
        chores.undo(k, c, db_path=db, today=MON)


# ---- ledger ----

def test_payout_spend_adjust_never_overdraw(db, no_mystery):
    k = kid(db)
    c = chore(db, k, value=300)
    do(db, k, c, MON)
    with pytest.raises(chores.ChoreError):
        chores.payout(k, 301, db_path=db)
    assert chores.payout(k, 100, note="cash", db_path=db)["balance_cents"] == 200
    assert chores.adjust(k, 50, note="gift", db_path=db)["balance_cents"] == 250
    with pytest.raises(chores.ChoreError):
        chores.adjust(k, -251, db_path=db)
    r = chores.create_reward("Movie night", 200, db_path=db)
    assert chores.redeem(k, r["id"], db_path=db)["balance_cents"] == 50
    with pytest.raises(chores.ChoreError):
        chores.redeem(k, r["id"], db_path=db)
    kinds = [row["kind"] for row in chores.ledger(k, db_path=db)]
    assert sorted(kinds) == ["adjust", "earn", "payout", "spend"]


# ---- bonuses ----

def test_streak_milestone_bonus_paid_once_and_removed_on_undo(db, no_mystery):
    k = kid(db)
    c = chore(db, k, value=0)
    chores.set_config("streak_milestones", '{"3": 40}', db_path=db)
    chores.set_config("perfect_week_bonus_cents", "0", db_path=db)
    do(db, k, c, MON)
    do(db, k, c, MON + timedelta(days=1))
    r = do(db, k, c, MON + timedelta(days=2))
    assert r["bonus_cents"] == 40 and chores.balance(k, db_path=db) == 40
    chores.undo(k, c, any_date=True, db_path=db, today=MON + timedelta(days=2))
    assert chores.balance(k, db_path=db) == 0
    r = do(db, k, c, MON + timedelta(days=2))
    assert r["bonus_cents"] == 40  # re-earned legitimately, not doubled
    assert chores.balance(k, db_path=db) == 40


def test_perfect_week_bonus(db, no_mystery):
    k = kid(db)
    c = chore(db, k, value=0)
    chores.set_config("streak_milestones", "{}", db_path=db)
    chores.set_config("perfect_week_bonus_cents", "100", db_path=db)
    last = None
    for i in range(7):
        last = do(db, k, c, MON + timedelta(days=i))
        if i < 6:
            assert last["bonus_cents"] == 0
    assert [b["kind"] for b in last["bonuses"]] == ["perfect_week"]
    assert chores.balance(k, db_path=db) == 100
    assert any(b["key"] == "perfect_week" and b["earned"] for b in chores.badges(k, db_path=db, today=SUN))


def test_mystery_bonus_deterministic_capped_and_toggle(db):
    k = kid(db)
    chores.set_config("mystery_chance_pct", "100", db_path=db)
    chores.set_config("mystery_max_cents", "25", db_path=db)
    chores.set_config("perfect_week_bonus_cents", "0", db_path=db)
    c1 = chore(db, k, "A", value=0)
    c2 = chore(db, k, "B", value=0)
    r1 = do(db, k, c1, MON)
    r2 = do(db, k, c2, MON)
    assert r1["bonus_cents"] in (5, 10, 15, 20, 25) and r2["bonus_cents"] == 0  # one per kid per day
    chores.undo(k, c1, db_path=db, today=MON)
    assert do(db, k, c1, MON)["bonus_cents"] == r1["bonus_cents"]  # same roll after undo/redo
    chores.set_config("mystery_enabled", "0", db_path=db)
    k2 = kid(db, "Claire")
    c3 = chore(db, k2, "C", value=0)
    assert do(db, k2, c3, MON)["bonus_cents"] == 0


def test_backfill_gets_base_pay_but_no_bonus(db):
    k = kid(db)
    c = chore(db, k)
    chores.set_config("mystery_chance_pct", "100", db_path=db)
    r = chores.complete(k, c, log_date=MON.isoformat(), db_path=db, today=MON + timedelta(days=3))
    assert r["earned_cents"] == 50 and r["bonus_cents"] == 0


def test_config_validation(db):
    for key, bad in (("nope", "1"), ("mystery_enabled", "2"), ("mystery_chance_pct", "101"),
                     ("perfect_week_bonus_cents", "-1"), ("streak_milestones", "[1]")):
        with pytest.raises(chores.ChoreError):
            chores.set_config(key, bad, db_path=db)


# ---- views ----

def test_kid_view_items_progress_and_week(db, no_mystery):
    k = kid(db)
    daily_id = chore(db, k, "Teeth")
    chore(db, k, "Trash", "weekly", value=100, days="tue")
    v = chores.kid_view(k, db_path=db, today=MON)
    assert [i["name"] for i in v["items"]] == ["Teeth"]  # trash not due Monday
    assert v["progress_pct"] == 0 and not v["all_done"]
    do(db, k, daily_id, MON)
    v = chores.kid_view(k, db_path=db, today=MON)
    assert v["all_done"] and v["progress_pct"] == 100 and v["earned_today_cents"] == 50
    assert v["week"][0]["is_today"] and v["week"][0]["perfect"] and v["week"][1]["future"]
    tue = chores.kid_view(k, db_path=db, today=MON + timedelta(days=1))
    assert {i["name"] for i in tue["items"]} == {"Teeth", "Trash"}


def test_flexible_weekly_chore_hidden_once_met(db, no_mystery):
    k = kid(db)
    c = chore(db, k, "Room", "weekly", target_count=1)
    assert len(chores.kid_view(k, db_path=db, today=MON)["items"]) == 1
    do(db, k, c, MON)
    assert chores.kid_view(k, db_path=db, today=MON)["items"][0]["done"]
    assert chores.kid_view(k, db_path=db, today=MON + timedelta(days=1))["items"] == []


def test_goal_progress_and_rewards_affordability(db, no_mystery):
    k = kid(db)
    c = chore(db, k, value=250)
    do(db, k, c, MON)
    chores.set_goal(k, "Lego set", 1000, db_path=db)
    chores.create_reward("Sticker", 100, db_path=db)
    chores.create_reward("Toy", 900, db_path=db)
    v = chores.kid_view(k, db_path=db, today=MON)
    assert v["goal"]["pct"] == 25 and not v["goal"]["reached"]
    assert [r["affordable"] for r in v["rewards"]] == [True, False]
    chores.clear_goal(k, db_path=db)
    assert chores.kid_view(k, db_path=db, today=MON)["goal"] is None


def test_badges_first_and_ten_chores(db, no_mystery):
    k = kid(db)
    c = chore(db, k)
    r = do(db, k, c, MON)
    assert [b["key"] for b in r["new_badges"]] == ["first_chore"]
    assert not any(b["earned"] for b in chores.badges(k, db_path=db, today=MON) if b["key"] == "ten_chores")


def test_week_summary_and_open_today(db, no_mystery):
    a, b = kid(db), kid(db, "Claire")
    ca = chore(db, a, "Teeth")
    chores.assign(ca, b, db_path=db)
    do(db, a, ca, MON)
    s = {r["kid"]: r for r in chores.week_summary(db_path=db, today=MON)}
    assert s["Ruth"]["owed_cents"] == 50 and s["Claire"]["owed_cents"] == 0
    assert s["Ruth"]["chores_done_this_week"] == 1
    assert [o["kid"] for o in chores.open_today(db_path=db, today=MON)] == ["Claire"]
    chores.set_config("nudge_enabled", "0", db_path=db)
    assert chores.open_today(db_path=db, today=MON) == []


# ---- CLI ----

@pytest.fixture
def cli(db, monkeypatch, capsys):
    monkeypatch.setattr(chores, "DB_PATH", db)

    def run(*argv):
        code = chores_store.main(list(argv))
        out = capsys.readouterr()
        return code, (json.loads(out.out) if out.out.strip() else None), out.err

    return run


def test_cli_end_to_end(cli):
    assert cli("kid", "add", "--name", "Ruth")[0] == 0
    code, chore_json, _ = cli("chore", "add", "--name", "Feed dog", "--frequency", "daily", "--value", "0.75",
                              "--kids", "Ruth")
    assert code == 0 and chore_json["value_cents"] == 75 and chore_json["kids"] == ["Ruth"]
    code, done, _ = cli("done", "--kid", "ruth", "--chore", "dog")
    assert code == 0 and done["earned_cents"] == 75
    assert cli("balance")[1][0]["balance_cents"] >= 75
    code, paid, _ = cli("payout", "--kid", "Ruth", "--amount", "0.50", "--note", "cash")
    assert code == 0 and paid["balance_cents"] >= 25
    code, _, err = cli("payout", "--kid", "Ruth", "--amount", "99")
    assert code == 2 and "cannot pay out" in json.loads(err)["error"]
    code, _, err = cli("done", "--kid", "nobody", "--chore", "dog")
    assert code == 1 and "error" in json.loads(err)
    assert cli("undo", "--kid", "Ruth", "--chore", "dog")[0] == 2  # already paid out
    assert cli("week-summary")[0] == 0 and cli("open")[0] == 0
    assert cli("config", "set", "--key", "mystery_enabled", "--value", "0")[0] == 0
