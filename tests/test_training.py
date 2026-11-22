import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import training  # noqa: E402
import training_store  # noqa: E402

TODAY = date(2026, 11, 18)  # a Wednesday; its Monday is 2026-11-16
MI = 1609.344


@pytest.fixture
def db(tmp_path):
    return tmp_path / "t.db"


def row(label, day, sport=100, miles=5.0, secs=3000, name="Run", hr=150):
    return {"labelId": label, "sportType": sport, "name": name, "date": day.replace("-", ""),
            "startTime": 1790000000, "startTimezone": -24, "distance": miles * MI,
            "workoutTime": secs, "totalTime": secs + 60, "avgHr": hr, "trainingLoad": 50,
            "avgCadence": 170, "ascent": 10}


def detail(zone_secs, percents=None, max_hr=180):
    total = sum(zone_secs) or 1
    percents = percents or [s / total * 100 for s in zone_secs]
    items = [{"zoneIndex": i, "leftScope": 100 + i * 10, "rightScope": 110 + i * 10, "second": s,
              "percent": p} for i, (s, p) in enumerate(zip(zone_secs, percents))]
    return {"zoneList": [{"type": 126, "zoneItemList": items}, {"type": 5, "zoneItemList": []}],
            "summary": {"maxHr": max_hr}}


ACCOUNT = {"zoneData": {"lthr": 171, "lthrZone": []}, "maxHr": 193}
EVEN = [0, 600, 1200, 600, 600, 0]  # 3000 s: 60% easy (z0-2), 40% z3+


def seed(db, rows, details=None, default=EVEN, **kw):
    details = details or {}

    def fd(coros_id, sport):
        d = details.get(coros_id, default)
        if isinstance(d, Exception):
            raise d
        return detail(d) if isinstance(d, list) else d

    return training.sync(db, today=TODAY, fetch_list_pages=lambda s, e: rows, fetch_detail=fd,
                         fetch_account=lambda: ACCOUNT, **kw)


# ---- sport grouping ----

def test_indoor_and_outdoor_group_as_running_walk_and_others_excluded(db):
    seed(db, [row("a", "2026-11-16", 100, 3), row("b", "2026-11-17", 101, 2),
              row("w", "2026-11-17", 101, 1, name="Indoor Walk"), row("c", "2026-11-17", 200, 20),
              row("s", "2026-11-17", 402, 0, name="Strength")])
    v = training.volume(db_path=db, today=TODAY)
    assert v["total"]["distance_mi"] == 5.0 and v["total"]["runs"] == 2
    cyc = training.volume(sports=["cycling"], db_path=db, today=TODAY)
    assert cyc["total"]["distance_mi"] == 20.0
    both = training.volume(sports="running,cycling", db_path=db, today=TODAY)
    assert both["total"]["distance_mi"] == 25.0
    walk = training.volume(sports=["walking"], db_path=db, today=TODAY)
    assert walk["total"]["runs"] == 1
    counts = {s["group"]: s["count"] for s in training.sports_available(db)}
    assert counts["running"] == 2 and counts["walking"] == 1 and counts["cycling"] == 1
    assert counts["strength"] == 1 and "hiking" not in counts


def test_raw_sport_id_selector_and_unknown_sport(db):
    seed(db, [row("a", "2026-11-16", 100, 3), row("b", "2026-11-17", 101, 2)])
    assert training.volume(sports=[101], db_path=db, today=TODAY)["total"]["distance_mi"] == 2.0
    with pytest.raises(training.TrainingError) as e:
        training.volume(sports=["swimming"], db_path=db, today=TODAY)
    assert e.value.code == 2


# ---- volume ----

def test_week_boundaries_sunday_vs_monday(db):
    seed(db, [row("sun", "2026-11-15", miles=4), row("mon", "2026-11-16", miles=6)])
    v = training.volume(db_path=db, today=TODAY)
    by = {r["bucket_start"]: r for r in v["rows"]}
    assert by["2026-11-09"]["distance_mi"] == 4.0
    assert by["2026-11-16"]["distance_mi"] == 6.0
    assert by["2026-11-16"]["bucket_end"] == "2026-11-22"


def test_default_range_is_12_zero_filled_weeks(db):
    seed(db, [row("a", "2026-11-16", miles=6)])
    v = training.volume(db_path=db, today=TODAY)
    assert len(v["rows"]) == 12
    assert v["rows"][0]["bucket_start"] == "2026-08-31" and v["rows"][-1]["bucket_start"] == "2026-11-16"
    assert all(r["distance_mi"] == 0 and r["runs"] == 0 for r in v["rows"][:-1])


def test_miles_conversion_and_metres(db):
    seed(db, [row("a", "2026-11-16", miles=3.1)])
    r = training.volume(db_path=db, today=TODAY)["rows"][-1]
    assert r["distance_mi"] == 3.1 and r["distance_m"] == round(3.1 * MI)


def test_distance_vs_time_metric_and_rolling_average(db):
    rows = [row(f"w{i}", d, miles=m, secs=s) for i, (d, m, s) in enumerate([
        ("2026-10-26", 10, 6000), ("2026-11-02", 20, 7200), ("2026-11-09", 30, 1800),
        ("2026-11-16", 40, 3600)])]
    seed(db, rows)
    dist = training.volume("distance", db_path=db, today=TODAY)
    last = dist["rows"][-1]
    assert last["rolling_avg"] == last["rolling_avg_distance_mi"] == 25.0  # (10+20+30+40)/4
    tm = training.volume("time", db_path=db, today=TODAY)
    assert tm["rows"][-1]["rolling_avg"] == tm["rows"][-1]["rolling_avg_time_s"] == 4650
    assert tm["rows"][-1]["time_s"] == 3600
    assert dist["total"]["distance_mi"] == 100.0
    assert dist["avg_per_week"]["distance_mi"] == round(100 / 12, 2)


def test_rolling_average_uses_history_before_range_start(db):
    seed(db, [row("a", "2026-11-02", miles=8), row("b", "2026-11-16", miles=4)])
    v = training.volume(start="2026-11-16", end="2026-11-18", db_path=db, today=TODAY)
    assert len(v["rows"]) == 1 and v["rows"][0]["rolling_avg_distance_mi"] == 3.0


# ---- zones ----

def test_zone_time_default_window_is_30_days_inclusive(db):
    seed(db, [row("in", "2026-10-20"), row("out", "2026-10-19")])  # window = 10-20 .. 11-18
    z = training.zone_time(db_path=db, today=TODAY)
    assert z["start"] == "2026-10-20" and z["end"] == "2026-11-18"
    assert len(z["rows"]) == 1
    assert z["rows"][0]["seconds"] == EVEN and z["rows"][0]["total_s"] == 3000
    assert z["rows"][0]["activities"] == 1


def test_zone_time_day_week_month_buckets_zero_filled(db):
    seed(db, [row("a", "2026-10-30"), row("b", "2026-11-02"), row("c", "2026-11-16")])
    days = training.zone_time("day", start="2026-11-01", end="2026-11-07", db_path=db, today=TODAY)
    assert len(days["rows"]) == 7
    assert days["rows"][1]["bucket_start"] == "2026-11-02" and days["rows"][1]["total_s"] == 3000
    assert days["rows"][0]["seconds"] == [0] * 6
    weeks = training.zone_time("week", start="2026-10-26", end="2026-11-18", db_path=db, today=TODAY)
    assert [r["bucket_start"] for r in weeks["rows"]] == ["2026-10-26", "2026-11-02", "2026-11-09",
                                                           "2026-11-16"]
    assert [r["total_s"] for r in weeks["rows"]] == [3000, 3000, 0, 3000]
    months = training.zone_time("month", start="2026-10-01", end="2026-11-18", db_path=db, today=TODAY)
    assert [r["bucket_start"] for r in months["rows"]] == ["2026-10-01", "2026-11-01"]
    assert [r["total_s"] for r in months["rows"]] == [3000, 6000]


def test_zone_share_sums_to_100_and_handles_empty(db):
    seed(db, [row("a", "2026-11-16", secs=3000)], default=[0, 700, 1100, 650, 550, 0])
    share = training.zone_share("day", start="2026-11-15", end="2026-11-16", db_path=db, today=TODAY)
    assert share["rows"][0]["percent"] is None and share["rows"][0]["total_s"] == 0
    p = share["rows"][1]["percent"]
    assert len(p) == 6 and round(sum(p), 1) == 100.0
    empty = training.zone_share(db_path=tmp_empty(db), today=TODAY)
    assert empty["rows"][0]["percent"] is None


def tmp_empty(db):
    return db.parent / "empty.db"


def test_easy_share_counts_zone0_and_separates_z1_z2(db):
    seed(db, [row("a", "2026-11-16", secs=3000)])  # EVEN: z0 0, z1 600, z2 1200, z3+ 1200
    e = training.easy_share("total", db_path=db, today=TODAY)
    assert e["total"]["easy_pct"] == 60.0 and e["total"]["z1_z2_pct"] == 60.0
    seed(db, [row("b", "2026-11-17", secs=3000)], default=[1000, 500, 500, 500, 500, 0])
    e = training.easy_share("total", db_path=db, today=TODAY)
    assert e["total"]["easy_pct"] == round(3800 / 6000 * 100, 1)  # zone 0 counts as easy
    assert e["total"]["z1_z2_pct"] == round(2800 / 6000 * 100, 1)
    wk = training.easy_share("week", start="2026-11-09", end="2026-11-18", db_path=db, today=TODAY)
    assert wk["rows"][0]["easy_pct"] is None and wk["rows"][1]["easy_pct"] is not None


def test_percent_normalisation_when_seconds_inconsistent(db):
    # seconds sum 2729 vs moving 2275 (>2% off); percent is the truth
    secs = [0, 500, 1000, 729, 500, 0]
    pct = [0, 20, 40, 20, 20, 0]
    seed(db, [row("a", "2026-11-16", secs=2275)], default=detail(secs, pct))
    z = training.zone_time(db_path=db, today=TODAY)["rows"][0]
    assert z["seconds"] == [0, 455, 910, 455, 455, 0] and z["total_s"] == 2275
    # consistent seconds (within 2%) are used as-is
    assert training.effective_zone_seconds(1000, [(1, 505, 50), (2, 505, 50)]) == [0, 505, 505, 0, 0, 0]
    assert training.effective_zone_seconds(1000, [(1, 400, 50), (2, 400, 50)]) == [0, 500, 500, 0, 0, 0]
    assert training.effective_zone_seconds(0, [(1, 400, 50)]) == [0, 400, 0, 0, 0, 0]


def test_longest_run_per_week(db):
    seed(db, [row("a", "2026-11-16", miles=3, secs=2400), row("b", "2026-11-17", miles=9, secs=4000),
              row("c", "2026-11-18", miles=5, secs=6000)])
    lr = training.longest_run_per_week(start="2026-11-09", end="2026-11-18", db_path=db, today=TODAY)
    assert lr["rows"][0] == {"bucket_start": "2026-11-09", "longest_distance": None, "longest_time": None}
    wk = lr["rows"][1]
    assert wk["longest_distance"]["coros_id"] == "b" and wk["longest_distance"]["distance_mi"] == 9.0
    assert wk["longest_time"]["coros_id"] == "c"


def test_summary_and_activities(db):
    seed(db, [row("a", "2026-11-16", miles=3, secs=3000), row("b", "2026-11-10", miles=6, secs=3000)])
    s = training.summary(db_path=db, today=TODAY)
    assert s["activities"] == 2 and s["total_distance_mi"] == 9.0 and s["total_time_s"] == 6000
    assert s["days"] == 30 and s["avg_weekly_distance_mi"] == round(9 / (30 / 7), 2)
    assert s["z3_plus_pct"] == 40.0 and s["easy_pct"] == 60.0
    acts = training.activities(db_path=db, today=TODAY)
    assert [a["coros_id"] for a in acts] == ["a", "b"]
    assert acts[0]["pace_s_per_mi"] == 1000 and acts[0]["zone_seconds"] == EVEN
    assert len(training.activities(limit=1, db_path=db)) == 1
    json.dumps([s, acts])


# ---- sync ----

def test_sync_is_idempotent_and_upserts(db):
    rows = [row("a", "2026-11-16", miles=3), row("b", "2026-11-17", miles=4)]
    r1 = seed(db, rows)
    assert r1["inserted"] == 2 and r1["detail_fetched"] == 2 and not r1["partial"]
    r2 = seed(db, rows)
    assert r2["inserted"] == 0 and r2["updated"] == 2 and r2["detail_attempted"] == 0
    changed = [row("a", "2026-11-16", miles=3.5), rows[1]]
    seed(db, changed)
    assert training.volume(db_path=db, today=TODAY)["rows"][-1]["distance_mi"] == 7.5
    c = training.connect(db)
    assert c.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 2
    assert c.execute("SELECT COUNT(*) FROM activity_zone_time").fetchone()[0] == 12
    c.close()


def test_sync_stores_local_start_time_max_hr_and_config(db):
    seed(db, [row("a", "2026-11-16")])
    c = training.connect(db)
    a = c.execute("SELECT * FROM activities").fetchone()
    c.close()
    assert a["start_local"] == "2026-09-21T08:13:20"  # 14:13:20 UTC minus 6h
    assert a["start_local"][:4] == "2026" and a["max_hr"] == 180 and a["is_indoor"] == 0
    assert training.get_config(db)["lthr"] == "171"
    defs = training.zone_definitions(db)
    assert defs["lthr"] == 171 and defs["max_hr"] == 193 and defs["zones"][2]["low"] == 120


def test_sync_tolerates_one_failing_detail_and_retries_later(db):
    rows = [row("a", "2026-11-16"), row("bad", "2026-11-17"), row("c", "2026-11-18")]
    r = seed(db, rows, details={"bad": RuntimeError("boom")})
    assert r["partial"] and r["detail_failed"] == 1 and r["detail_fetched"] == 2
    assert "bad" in r["errors"][0] and "boom" in r["errors"][0]
    assert training.volume(db_path=db, today=TODAY)["total"]["runs"] == 3  # activity still saved
    r2 = seed(db, rows)  # only the failed one is retried
    assert r2["detail_attempted"] == 1 and r2["detail_fetched"] == 1 and not r2["partial"]


def test_sync_detail_only_for_configured_sports_unless_all(db):
    rows = [row("run", "2026-11-16"), row("bike", "2026-11-16", 200, 10)]
    fetched = []

    def fd(cid, sport):
        fetched.append(cid)
        return detail(EVEN)

    kw = dict(fetch_list_pages=lambda s, e: rows, fetch_detail=fd, fetch_account=lambda: ACCOUNT, today=TODAY)
    training.sync(db, **kw)
    assert fetched == ["run"]
    training.sync(db, all_sports=True, **kw)
    assert fetched == ["run", "bike"]


def test_sync_window_incremental_vs_full(db):
    windows = []

    def lp(s, e):
        windows.append((s, e))
        return [row("a", "2026-11-16")]

    kw = dict(fetch_list_pages=lp, fetch_detail=lambda c, s: detail(EVEN), fetch_account=lambda: ACCOUNT,
              today=TODAY)
    training.sync(db, **kw)
    assert windows[0] == (date(2015, 1, 1), TODAY)
    training.sync(db, **kw)
    assert windows[1] == (date(2026, 11, 2), TODAY)  # latest day minus 14
    training.sync(db, full=True, **kw)
    assert windows[2][0] == date(2015, 1, 1)


def test_sync_list_failure_raises_code_3_and_account_failure_is_partial(db):
    def boom(s, e):
        raise RuntimeError("login expired")

    with pytest.raises(training.TrainingError) as e:
        training.sync(db, today=TODAY, fetch_list_pages=boom, fetch_detail=lambda c, s: {},
                      fetch_account=lambda: {})
    assert e.value.code == 3

    def bad_account():
        raise RuntimeError("no account")

    r = training.sync(db, today=TODAY, fetch_list_pages=lambda s, e: [row("a", "2026-11-16")],
                      fetch_detail=lambda c, s: detail(EVEN), fetch_account=bad_account)
    assert r["partial"] and r["detail_fetched"] == 1


def test_threshold_change_is_picked_up(db):
    seed(db, [row("a", "2026-11-16")])
    acct = {"zoneData": {"lthr": 175}, "maxHr": 190}
    training.sync(db, today=TODAY, fetch_list_pages=lambda s, e: [], fetch_detail=lambda c, s: {},
                  fetch_account=lambda: acct)
    d = training.zone_definitions(db)
    assert d["lthr"] == 175 and d["max_hr"] == 190 and d["updated_at"]


# ---- config + validation ----

def test_config_roundtrip_and_default_sports_drive_default_selection(db):
    seed(db, [row("a", "2026-11-16", miles=3), row("b", "2026-11-16", 200, 10)])
    assert training.get_config(db)["default_sports"] == "running"
    training.set_config("default_sports", "running,cycling", db)
    assert training.volume(db_path=db, today=TODAY)["total"]["distance_mi"] == 13.0
    assert {s["group"]: s["default"] for s in training.sports_available(db)}["cycling"] is True


@pytest.mark.parametrize("call", [
    lambda db: training.volume(metric="calories", db_path=db, today=TODAY),
    lambda db: training.volume(bucket="day", db_path=db, today=TODAY),
    lambda db: training.zone_time(bucket="year", db_path=db, today=TODAY),
    lambda db: training.zone_time(start="2026-13-01", db_path=db, today=TODAY),
    lambda db: training.zone_time(start="2026-11-10", end="2026-11-01", db_path=db, today=TODAY),
    lambda db: training.summary(start="nope", db_path=db, today=TODAY),
    lambda db: training.activities(limit=0, db_path=db),
    lambda db: training.set_config("bogus", "1", db),
    lambda db: training.set_config("lthr", "abc", db),
    lambda db: training.set_config("default_sports", "swimming", db),
])
def test_validation_errors_are_code_2(db, call):
    with pytest.raises(training.TrainingError) as e:
        call(db)
    assert e.value.code == 2


# ---- CLI ----

def run_cli(monkeypatch, capsys, db, *argv):
    real = training.connect
    monkeypatch.setattr(training, "DB_PATH", db)
    monkeypatch.setattr(training, "connect", lambda p=None: real(p or db))
    code = training_store.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_commands_print_json(monkeypatch, capsys, db):
    seed(db, [row("a", "2026-11-16", miles=3)])
    for argv in (["sports"], ["zone-defs"], ["config", "get"], ["activities", "--limit", "5"],
                 ["volume", "--metric", "time"], ["zones", "--bucket", "week", "--start", "2020-01-01",
                                                  "--end", "2020-01-31"],
                 ["share"], ["easy"], ["longest"], ["summary", "--sports", "running,cycling"]):
        code, out, err = run_cli(monkeypatch, capsys, db, *argv)
        assert code == 0, (argv, err)
        json.loads(out)


def test_cli_errors_go_to_stderr_with_exit_codes(monkeypatch, capsys, db):
    code, out, err = run_cli(monkeypatch, capsys, db, "summary", "--start", "garbage")
    assert code == 2 and out == "" and "error" in json.loads(err)
    code, out, err = run_cli(monkeypatch, capsys, db, "summary", "--sports", "nope")
    assert code == 2
    code, out, err = run_cli(monkeypatch, capsys, db, "config", "set", "--key", "lthr")
    assert code == 2


def test_cli_config_set_and_sync_exit_code(monkeypatch, capsys, db):
    code, out, _ = run_cli(monkeypatch, capsys, db, "config", "set", "--key", "default_sports",
                           "--value", "running,hiking")
    assert code == 0 and json.loads(out)["default_sports"] == "running,hiking"
    monkeypatch.setattr(training, "sync", lambda **kw: {"partial": True, "errors": ["x"]})
    code, out, _ = run_cli(monkeypatch, capsys, db, "sync", "--all-sports")
    assert code == 3 and json.loads(out)["partial"]
    monkeypatch.setattr(training, "sync", lambda **kw: {"partial": False, "errors": []})
    assert run_cli(monkeypatch, capsys, db, "sync", "--full")[0] == 0
