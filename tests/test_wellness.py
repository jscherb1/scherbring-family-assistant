import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "fitness"))
sys.path.insert(0, str(ROOT))

import dayrating  # noqa: E402
import habits  # noqa: E402
import insights  # noqa: E402
import wellness  # noqa: E402
import wellness_store  # noqa: E402
from insight_sources import coros_wellness  # noqa: E402

TODAY = date(2026, 10, 14)
MORNING = datetime(2026, 10, 14, 10, 0)
EVENING = datetime(2026, 10, 14, 21, 0)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "t.db"


def put(db, metric, values: dict, source="coros_health", at="2026-10-14T09:00:00"):
    conn = wellness.connect(db)
    for day, v in values.items():
        conn.execute("INSERT OR REPLACE INTO insight_daily (metric, day, value, source, updated_at) VALUES (?,?,?,?,?)",
                     (metric, day.isoformat() if isinstance(day, date) else day, v, source, at))
    conn.commit()
    conn.close()


def back(n):
    return TODAY - timedelta(days=n)


def rate(db, score, day, note=None):
    dayrating.set_rating(score, note, rating_date=day.isoformat(), db_path=db, today=TODAY)


# ---- timing ----

def test_point_status_rules():
    t = TODAY.isoformat()
    y = back(1).isoformat()
    assert wellness.point_status("sleep_hours", t, False, t, y) == "pending"      # last night not synced
    assert wellness.point_status("sleep_hours", t, True, t, t) == "final"
    assert wellness.point_status("steps", t, True, t, t) == "partial"            # today still accumulating
    assert wellness.point_status("steps", t, False, t, y) == "pending"
    assert wellness.point_status("rating", t, False, t, y) == "pending"           # rated in the evening
    assert wellness.point_status("rating", y, False, t, back(2).isoformat()) == "missing"
    assert wellness.point_status("hrv", y, False, t, back(3).isoformat()) == "pending"   # after newest sync
    assert wellness.point_status("hrv", back(5).isoformat(), False, t, y) == "missing"   # real gap


def test_mood_shows_yesterday_until_rated(db):
    rate(db, 7, back(1), "fine")
    m = wellness.today_view(now=MORNING, db_path=db)["mood"]
    assert m["shown"]["score"] == 7 and m["shown_when"] == "Yesterday" and m["today_state"] == "later"
    assert wellness.today_view(now=EVENING, db_path=db)["mood"]["today_state"] == "due"
    rate(db, 9, TODAY)
    m = wellness.today_view(now=EVENING, db_path=db)["mood"]
    assert m["shown"]["score"] == 9 and m["shown_when"] == "Today" and m["today_state"] == "rated"


def test_last_night_falls_back_until_synced(db):
    put(db, "coros:sleep_hours", {back(1): 7.5})
    put(db, "coros:sleep_score", {back(1): 80})
    v = wellness.today_view(now=MORNING, db_path=db)
    assert v["sleep"]["status"] == "pending" and v["sleep"]["day"] == back(1).isoformat()
    assert v["rings"]["sleep"]["status"] == "pending"
    put(db, "coros:sleep_hours", {TODAY: 6.0})
    v = wellness.today_view(now=MORNING, db_path=db)
    assert v["sleep"]["status"] == "final" and v["sleep"]["values"]["sleep_hours"] == 6.0


def test_series_excludes_partial_today_and_zero_scores(db):
    put(db, "coros:steps", {back(i): 8000 for i in range(1, 8)} | {TODAY: 500})
    s = wellness.series("steps", 7, today=TODAY, db_path=db)
    assert s["points"][-1]["status"] == "partial"
    assert s["summary"]["avg"] == 8000 and s["summary"]["days"] == 6   # the partial 500 is left out
    assert s["avg7"][-1]["value"] == 8000
    put(db, "coros:sleep_score", {back(1): 0, back(2): 80})
    sc = wellness.series("sleep_score", 3, today=TODAY, db_path=db)
    assert [p["value"] for p in sc["points"]] == [80, None, None]


def test_rolling_and_baseline_use_final_days_only():
    window = [back(i).isoformat() for i in range(4, -1, -1)]
    final = {window[0]: 2.0, window[1]: 4.0, window[2]: 6.0}
    roll = wellness.rolling(final, window)
    assert roll[1]["value"] is None and roll[2]["value"] == 4.0 and roll[4]["value"] == 4.0
    assert wellness.baseline([1.0] * 4) is None
    b = wellness.baseline([10.0, 12.0, 14.0, 10.0, 14.0])
    assert b["mean"] == 12.0 and b["low"] < 12 < b["high"]


# ---- readiness and vitals ----

def test_readiness_score():
    assert wellness.readiness_score(None, None, None) is None
    assert wellness.readiness_score(0, 0, 50) == 50
    assert wellness.readiness_score(2, -2, 100) == 100          # clamped
    assert wellness.readiness_score(-1, None, None) == 25       # re-weighted over what's available
    assert wellness.zone(70) == "good" and wellness.zone(50) == "mid" and wellness.zone(10) == "bad"


def test_vitals_and_estimated_recovery(db):
    put(db, "coros:hrv_ms", {back(i): 50.0 + (i % 3) for i in range(1, 25)} | {TODAY: 35.0}, source="coros_wellness")
    put(db, "coros:resting_hr", {back(i): 50.0 + (i % 2) for i in range(1, 25)} | {TODAY: 50.0}, source="coros_wellness")
    put(db, "coros:sleep_score", {TODAY: 60})
    put(db, "coros:sleep_hours", {TODAY: 7})
    v = wellness.today_view(now=MORNING, db_path=db)
    hrv = next(x for x in v["vitals"] if x["key"] == "hrv")
    assert hrv["range"] == "below" and hrv["verdict"] == "bad" and hrv["when"] == "Last night"
    assert "HRV" in v["out_of_range"]
    rec = v["rings"]["recovery"]
    assert rec["source"] == "estimate" and rec["zone"] in ("bad", "mid") and rec["value"] < 50
    put(db, "coros:recovery_pct", {TODAY: 88})
    rec = wellness.today_view(now=MORNING, db_path=db)["rings"]["recovery"]
    assert rec == {**rec, "value": 88, "source": "coros", "zone": "good"}


def test_strain_against_recommended_range(db):
    put(db, "coros:load_7d", {back(1): 250}, source="coros_wellness")
    put(db, "coros:load_rec_min", {back(1): 300}, source="coros_wellness")
    put(db, "coros:load_rec_max", {back(1): 550}, source="coros_wellness")
    st = wellness.today_view(now=MORNING, db_path=db)["rings"]["strain"]
    assert st["value"] == 250 and st["zone"] == "low"


def test_freshness_flags_stale_sources(db):
    put(db, "coros:steps", {back(1): 1}, at="2026-10-14T09:30:00")
    put(db, "coros:hrv_ms", {back(1): 40}, source="coros_wellness", at="2026-10-01T09:00:00")
    f = {x["source"]: x for x in wellness.today_view(now=MORNING, db_path=db)["freshness"]}
    assert f["coros_health"]["stale"] is False and f["coros_wellness"]["stale"] is True


def test_habits_in_today_view(db):
    habits.create_habit("Stretch", "daily", 1, db_path=db)
    h = wellness.today_view(now=MORNING, db_path=db)["habits"]
    assert h[0]["name"] == "Stretch" and h[0]["status"] == "open"


# ---- trends, heatmap, drivers, scatter ----

def test_trends_shape_and_bands(db):
    put(db, "coros:hrv_ms", {back(i): 40.0 + i % 5 for i in range(0, 60)}, source="coros_wellness")
    put(db, "coros:hrv_base_low", {back(0): 35.0}, source="coros_wellness")
    put(db, "coros:hrv_base_high", {back(0): 50.0}, source="coros_wellness")
    put(db, "coros:deep_pct", {back(0): 20})
    put(db, "coros:rem_pct", {back(0): 20})
    put(db, "coros:light_pct", {back(0): 55})
    t = wellness.trends(30, now=MORNING, db_path=db)
    assert len(t["dates"]) == 30 and t["end"] == TODAY.isoformat()
    assert t["hrv_band"][-1] == {"date": TODAY.isoformat(), "low": 35.0, "high": 50.0, "source": "coros"}
    assert t["hrv_band"][0]["source"] == "rolling" and t["hrv_band"][0]["low"] is not None
    assert t["stages"] == [{"date": TODAY.isoformat(), "deep": 20, "rem": 20, "light": 55, "awake": 5.0}]
    with pytest.raises(wellness.WellnessError):
        wellness.trends(7, db_path=db)


def test_heatmap_levels_and_direction(db):
    put(db, "coros:resting_hr", {back(i): 40.0 + i for i in range(1, 30)}, source="coros_wellness")
    h = wellness.heatmap("resting_hr", days=35, now=MORNING, db_path=db)
    cells = {c["date"]: c for w in h["weeks"] for c in w}
    assert all(len(w) <= 7 for w in h["weeks"]) and date.fromisoformat(h["weeks"][0][0]["date"]).weekday() == 0
    assert cells[back(1).isoformat()]["level"] == 5     # lowest resting HR is the best day
    assert cells[back(29).isoformat()]["level"] == 1
    assert cells[TODAY.isoformat()]["level"] == 0
    with pytest.raises(wellness.WellnessError):
        wellness.heatmap("nope", db_path=db)


def test_drivers_rating_matches_insights(db):
    for i in range(12):
        rate(db, 8 if i % 2 else 4, back(i))
        put(db, "habit:Run", {back(i): 1.0 if i % 2 else 0.0}, source="habits")
    d = wellness.drivers("rating", now=MORNING, db_path=db)
    stats = insights.analyze(days=90, db_path=db, today=TODAY)
    assert [f["metric"] for f in d["findings"]] == [f["metric"] for f in stats["findings"]]
    run = next(f for f in d["findings"] if f["metric"] == "habit:Run")
    assert run["label"] == "Run (habit)" and run["effect"] == 4.0
    with pytest.raises(wellness.WellnessError):
        wellness.drivers("mood", db_path=db)


def test_analyze_against_other_target_excludes_itself(db):
    put(db, "coros:hrv_ms", {back(i): 30.0 + 10 * (i % 2) for i in range(14)}, source="coros_wellness")
    put(db, "coros:hrv_vs_baseline", {back(i): float(i % 2) for i in range(14)}, source="coros_wellness")
    put(db, "habit:Alcohol", {back(i): 0.0 if i % 2 else 1.0 for i in range(14)}, source="habits")
    put(db, "coros:hrv_base_low", {back(i): 30.0 + i for i in range(14)}, source="coros_wellness")
    d = wellness.drivers("hrv", now=MORNING, db_path=db)
    metrics = {f["metric"] for f in d["findings"]}
    assert "habit:Alcohol" in metrics
    assert not metrics & {"coros:hrv_ms", "coros:hrv_vs_baseline", "coros:hrv_base_low"}
    assert d["target_days"] == 14 and d["unit"] == "ms"


def test_scatter_pairs_final_days(db):
    for i in range(1, 6):
        rate(db, 4 + i, back(i))
        put(db, "coros:sleep_hours", {back(i): 5.0 + i})
    sc = wellness.scatter("sleep_hours", "rating", now=MORNING, db_path=db)
    assert sc["n"] == 5 and sc["r"] == 1.0 and sc["fit"]["slope"] == pytest.approx(1.0)
    with pytest.raises(wellness.WellnessError):
        wellness.scatter("rating", "rating", db_path=db)
    with pytest.raises(wellness.WellnessError):
        wellness.scatter("sleep_hours", "rating", days=2, db_path=db)


def test_fmt_and_labels():
    assert wellness.fmt("bedtime", 11.0) == "11:00 PM"
    assert wellness.fmt("wake_time", 6.5) == "6:30 AM"
    assert wellness.fmt("sleep_hours", 7.3) == "7.3 h" and wellness.fmt("steps", 12345) == "12,345"
    assert wellness.fmt("rem_pct", 18) == "18%" and wellness.fmt("hrv", None) == "—"
    assert wellness.label_for("coros:hrv_ms (previous day)") == "HRV (day before)"
    assert wellness.label_for("weather:precip_in") == "Rain" and wellness.label_for("day:Monday") == "Monday"


# ---- new COROS fields ----

def test_coros_wellness_keeps_new_day_detail_fields():
    out = coros_wellness.to_metrics([{
        "happenDay": 20261013, "avgSleepHrv": 48, "sleepHrvBase": 42, "sleepHrvIntervalList": [5, 25, 34, 50],
        "rhr": 50, "ati": 15.0, "cti": 46.0, "t28d": 513, "recomendTlMin": 322.0, "recomendTlMax": 579.6,
        "tiredRateNew": -31.0, "tiredRateStateNew": 1, "trainingLoad": 0, "trainingLoadRatio": 0.32,
        "trainingLoadRatioState": 1, "t7d": 124, "cycleVo2max": 0}])
    d = "2026-10-13"
    assert out["coros:ati"][d] == 15.0 and out["coros:cti"][d] == 46.0 and out["coros:load_28d"][d] == 513
    assert out["coros:load_rec_min"][d] == 322.0 and out["coros:load_rec_max"][d] == 579.6
    assert "coros:hrv_base_low" not in out and "coros:hrv_vs_baseline" not in out  # HRV band comes from the MCP now
    assert out["coros:training_load"][d] == 0.0
    assert "coros:vo2max" not in out
    assert {"coros:load_rec_min", "coros:hrv_base_low", "coros:fatigue_state"} <= insights.NOT_ANALYZED


def test_load_freshness_shows_weekly_and_live_load(db):
    put(db, "coros:load_7d", {back(6): 250}, source="coros_wellness")
    empty = wellness.today_view(now=MORNING, db_path=db)["load_freshness"]
    assert empty["live"]["day"] is None and empty["weekly"]["age_days"] == 6
    put(db, "coros:mcp_load_short", {back(0): 32, back(1): 24})
    put(db, "coros:mcp_load_long", {back(0): 47})
    put(db, "coros:mcp_load_ratio", {back(0): 0.68})
    lf = wellness.today_view(now=MORNING, db_path=db)["load_freshness"]
    assert lf["live"] == {"day": TODAY.isoformat(), "age_days": 0, "short": 32.0, "long": 47.0, "ratio": 0.68}
    assert lf["weekly"]["day"] == back(6).isoformat()
    assert wellness.METRICS["mcp_load_short"]["metric"] == "coros:mcp_load_short"


# ---- CLI ----

def run_cli(monkeypatch, capsys, db, *argv):
    monkeypatch.setattr(wellness.dayrating, "DB_PATH", db)
    code = wellness_store.main(list(argv))
    out = capsys.readouterr()
    return code, (json.loads(out.out) if out.out.strip() else None), out.err


def test_cli(monkeypatch, capsys, db):
    put(db, "coros:sleep_hours", {date.today() - timedelta(days=1): 7.0})
    code, out, _ = run_cli(monkeypatch, capsys, db, "today")
    assert code == 0 and "rings" in out
    code, out, _ = run_cli(monkeypatch, capsys, db, "metric", "--key", "sleep_hours", "--days", "3")
    assert code == 0 and len(out["points"]) == 3
    code, out, _ = run_cli(monkeypatch, capsys, db, "trends", "--range", "30")
    assert code == 0 and out["range"] == 30
    code, out, _ = run_cli(monkeypatch, capsys, db, "metrics")
    assert code == 0 and any(m["key"] == "hrv" for m in out)
    code, _, err = run_cli(monkeypatch, capsys, db, "metric", "--key", "nope")
    assert code == 2 and "unknown metric" in json.loads(err)["error"]
    code, _, err = run_cli(monkeypatch, capsys, db, "trends", "--range", "12")
    assert code == 2
    code, _, err = run_cli(monkeypatch, capsys, db, "drivers", "--target", "x")
    assert code == 2
