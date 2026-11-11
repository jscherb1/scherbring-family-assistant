"""Rivian (R2): shared service rules, CLI, and UI/service parity (HA stubbed, no network)."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ha  # noqa: E402
import rivian  # noqa: E402
import rivian_store  # noqa: E402

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def S(entity_id, state, updated=None, **attrs):
    ts = (updated or NOW).isoformat()
    return {"entity_id": entity_id, "state": state, "attributes": attrs, "last_changed": ts, "last_updated": ts}


def base_states(**overrides):
    st = {
        "sensor.r2_battery_state_of_charge": "54.1", "sensor.r2_battery_state_of_charge_limit": "80",
        "sensor.r2_battery_capacity": "91.2", "sensor.r2_estimated_vehicle_range": "163.4",
        "sensor.r2_range_threshold": "normal", "sensor.r2_cold_range_notification": "normal_range",
        "binary_sensor.r2_charger_connection": "on", "binary_sensor.r2_charging_status": "on",
        "sensor.r2_charger_state": "charging_active", "sensor.r2_charging_speed": "7.6",
        "sensor.r2_charging_rate": "24.8", "sensor.r2_charging_time_remaining": "150",
        "sensor.r2_charging_energy_delivered": "3.2", "sensor.r2_charging_range_added": "10.4",
        "sensor.r2_charging_time_elapsed": "1800", "sensor.r2_charger_derate_status": "cell_thermal_lim_cold",
        "sensor.r2_charging_fault": "none", "switch.r2_charging_schedule_enabled": "off",
        "time.r2_charging_schedule_start": "00:00:00", "time.r2_charging_schedule_end": "06:00:00",
        "sensor.r2_power_state": "ready", "sensor.r2_gear_selector": "park", "sensor.r2_drive_mode": "everyday",
        "sensor.r2_speed": "0", "sensor.r2_active_driver": "Justin S.", "sensor.r2_odometer": "1234.5",
        "binary_sensor.r2_locked_state": "on",  # lock class: on = unlocked
        "binary_sensor.r2_door_rear_left": "on", "binary_sensor.r2_door_front_left": "off",
        "binary_sensor.r2_door_front_left_lock": "off", "binary_sensor.r2_front_trunk": "off",
        "binary_sensor.r2_liftgate": "off", "binary_sensor.r2_window_front_right": "off",
        "sensor.r2_tire_pressure_front_left": "40.1", "sensor.r2_tire_pressure_front_left_status": "ok",
        "sensor.r2_tire_pressure_front_right": "31.0", "sensor.r2_tire_pressure_front_right_status": "warning_soft",
        "sensor.r2_tire_pressure_rear_left": "40.0", "sensor.r2_tire_pressure_rear_left_status": "ok",
        "sensor.r2_tire_pressure_rear_right": "39.8", "sensor.r2_tire_pressure_rear_right_status": "ok",
        "sensor.r2_12v_battery_health": "normal", "sensor.r2_battery_thermal_status": "nominal",
        "sensor.r2_brake_fluid_level": "inactive", "binary_sensor.r2_wiper_fluid_level": "on",
        "sensor.r2_cabin_temperature": "68.4", "binary_sensor.r2_vehicle_updates": "off",
        "sensor.r2_software_ota_status": "idle", "image.r2_three_quarter_dark": "x",
    }
    st.update(overrides)
    out = [S(k, v) for k, v in st.items() if v is not None]
    out.append(S("device_tracker.r2", "home", latitude=44.0, longitude=-92.4))
    out.append(S("update.r2_software", "off", installed_version="2026.36.42", latest_version="2026.36.42"))
    return out


def _ms(day_offset: int, hour: int = 0) -> int:
    local = datetime.now(ha.local_tz()).replace(hour=hour, minute=0, second=0, microsecond=0) - timedelta(days=day_offset)
    return int(local.timestamp() * 1000)


DAILY = {
    "sensor.r2_battery_state_of_charge": [{"start": _ms(1), "mean": 70.0, "min": 60.0, "max": 80.0},
                                          {"start": _ms(0), "mean": 57.0, "min": 50.0, "max": 60.0}],
    "sensor.r2_estimated_vehicle_range": [{"start": _ms(1), "mean": 230.4}, {"start": _ms(0), "mean": 170.6}],
    "sensor.r2_odometer": [{"start": _ms(1), "change": 30.0}, {"start": _ms(0), "change": 12.0}],
}

# A synthetic day of raw history (local time on FIX's day). One 12-mile trip 07:30-07:50 with a 30 s stop in park
# at 07:35 (must not split it), a garage shuffle at 09:00 (no miles: dropped), plugged in overnight and again at 17:00.
FIX = datetime.now(ha.local_tz()).replace(hour=18, minute=0, second=0, microsecond=0)


def L(h, m=0, s=0):
    return FIX.replace(hour=h, minute=m, second=s).astimezone(timezone.utc).isoformat()


HISTORY_EVENTS = {
    "sensor.r2_gear_selector": [(L(0), "park"), (L(7, 30), "drive"), (L(7, 35), "park"), (L(7, 35, 30), "drive"),
                                (L(7, 50), "park"), (L(9), "reverse"), (L(9, 0, 20), "park")],
    "sensor.r2_odometer": [(L(0), "100.0"), (L(7, 49), "112.0")],
    "sensor.r2_battery_state_of_charge": [(L(0), "80.0"), (L(7, 40), "78.0"), (L(7, 50, 30), "76.0"),
                                          (L(7, 51), "75.9"), (L(7, 55), "75.8"), (L(16), "75.0")],
    "sensor.r2_altitude": [(L(0), "1000"), (L(7, 49), "1100")],
    "sensor.r2_drive_mode": [(L(0), "everyday"), (L(7, 40), "sport")],
    "binary_sensor.r2_charger_connection": [(L(0), "on"), (L(7), "off"), (L(17), "on")],
    "sensor.openweathermap_temperature": [(L(0), "50.0")],
}


def history_payload():
    out = []
    for entity, events in HISTORY_EVENTS.items():
        rows = [{"state": v, "last_changed": t} for t, v in events]
        rows[0]["entity_id"] = entity
        out.append(rows)
    return out


@pytest.fixture
def fake_ha(monkeypatch, tmp_path):
    monkeypatch.setattr(rivian, "DB_PATH", tmp_path / "rivian.db")
    holder = {"states": base_states(), "calls": []}

    def fake_open(path, body=None, method=None, raw=False):
        holder["calls"].append(path)
        if path == "/api/states":
            return holder["states"]
        if path.startswith("/api/history/period/"):
            return history_payload()
        if path.startswith("/api/image_proxy/"):
            return b"PNG", "image/png"
        if path.startswith("/api/services/"):
            raise AssertionError("the Rivian service must never call an HA service")
        return []

    monkeypatch.setattr(ha, "_open", fake_open)
    monkeypatch.setattr(ha, "ws_commands", lambda cmds: [DAILY])
    ha.clear_cache()
    return holder


# ---- service rules ----

def test_status_energy_charging_and_labels(fake_ha):
    s = rivian.status()
    e, ch = s["energy"], s["charging"]
    assert e["soc"] == 54.1 and e["soc_display"] == "54%" and e["limit"] == 80 and e["range_mi"] == 163
    assert e["kwh_remaining"] == 49.3 and e["zone"] == "good"
    assert ch["active"] and ch["label"] == "Charging" and ch["kw"] == 7.6 and ch["remaining_display"] == "2 h 30 min"
    assert ch["derate_text"] == "The battery is cold, so charging is slowed."
    eta = datetime.fromisoformat(ch["eta"])
    assert abs((eta - datetime.now(timezone.utc)).total_seconds() - 150 * 60) < 120
    assert s["vehicle"]["drive_mode"] == "All-Purpose" and s["vehicle"]["location"] == "Home"
    assert s["image"] == "three_quarter" and s["images"] == ["three_quarter"]


def test_closures_tires_and_health_exceptions(fake_ha):
    s = rivian.status()
    cl = s["closures"]
    assert cl["locked"] is False and cl["open"] == ["Rear left door"] and cl["all_closed"] is False
    assert cl["doors"][0]["locked"] is True  # front-left lock "off" = locked
    fr = next(t for t in s["tires"] if t["key"] == "front_right")
    assert fr["zone"] == "mid" and fr["status_label"] == "Low" and not fr["ok"]
    titles = [h["title"] for h in s["health"]]
    assert "Front right tire: low" in titles and "Washer fluid is low" in titles
    assert s["health_ok"] is False


def test_low_soc_is_bad_zone_and_all_normal_when_clean(fake_ha):
    fake_ha["states"] = base_states(**{"sensor.r2_battery_state_of_charge": "15",
                                       "sensor.r2_tire_pressure_front_right_status": "ok",
                                       "binary_sensor.r2_wiper_fluid_level": "off"})
    s = rivian.status()
    assert s["energy"]["zone"] == "bad" and s["health"] == [] and s["health_ok"]


def test_stale_and_long_eta(fake_ha):
    old = NOW - timedelta(hours=9)
    fake_ha["states"] = [dict(st, last_updated=old.isoformat(), last_changed=old.isoformat())
                         for st in base_states(**{"sensor.r2_charging_time_remaining": "6000"})]
    s = rivian.status()
    assert s["stale"] and any(h["title"] == "Car hasn't reported in a while" for h in s["health"])
    assert s["charging"]["eta"] is None and s["charging"]["remaining_display"] == "3+ days at this rate"


def test_missing_entities_degrade_gracefully(fake_ha):
    fake_ha["states"] = [S("sensor.r2_battery_state_of_charge", "unavailable")]
    s = rivian.status()
    assert s["energy"]["soc"] is None and s["energy"]["soc_display"] == "—"
    assert s["closures"]["locked"] is None and s["closures"]["all_closed"] is None
    assert s["charging"]["label"] == "Not plugged in"


def test_no_car_and_unreachable_are_not_found(fake_ha, monkeypatch):
    fake_ha["states"] = [S("sensor.other", "1")]
    with pytest.raises(rivian.RivianError) as exc:
        rivian.status()
    assert exc.value.code == 1

    def offline(*_a, **_k):
        raise ha.HAError("Can't reach Home Assistant (x)", 1)
    monkeypatch.setattr(ha, "_open", offline)
    ha.clear_cache()
    with pytest.raises(rivian.RivianError) as exc:
        rivian.status()
    assert exc.value.code == 1 and "Can't reach" in exc.value.message


def test_trends_miles_soc_range(fake_ha):
    t = rivian.trends(7)
    today = datetime.now(ha.local_tz()).date()
    y, d0 = (today - timedelta(days=1)).isoformat(), today.isoformat()
    assert len(t["dates"]) == 7 and t["dates"][-1] == d0
    assert t["miles"] == {y: 30.0, d0: 12.0}
    assert t["soc"]["min"][y] == 60.0 and t["range_mi"][d0] == 171
    assert t["totals"]["miles_7d"] == 42.0 and t["data_days"] == 2 and t["limit"] == 80
    assert rivian.trends(90)["days"] == 90


def test_trends_rejects_bad_days(fake_ha):
    with pytest.raises(rivian.RivianError) as exc:
        rivian.trends(9)
    assert exc.value.code == 2


# ---- trips and parked drain ----

def test_segments_debounce_truncation_and_in_progress():
    t0 = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
    T = lambda m: t0 + timedelta(minutes=m)  # noqa: E731
    g = rivian._Series([(T(0), "park"), (T(10), "drive"), (T(20), "park"), (T(20.5), "drive"), (T(30), "park"),
                        (T(40), "not_defined"), (T(50), "reverse"), (T(55), "park")], numeric=False)
    assert rivian.segments(g, T(0), T(60)) == [(T(10), T(30)), (T(50), T(55))]
    assert rivian.segments(g, T(0), T(55.5)) == [(T(10), T(30))]  # last one parked < 60 s ago: still open
    under_way = rivian._Series([(T(0), "drive"), (T(5), "park"), (T(10), "drive"), (T(15), "park")], numeric=False)
    assert rivian.segments(under_way, T(0), T(60)) == [(T(10), T(15))]


def test_sync_saves_trip_and_drain(fake_ha, tmp_path):
    db = tmp_path / "t.db"
    r = rivian.sync(1, now=FIX, db_path=db)
    assert r["trips"] == 1 and r["drain_days"] == 1 and not r["in_progress"]
    rivian.sync(1, now=FIX, db_path=db)  # idempotent: upserts, no duplicate
    t = rivian.trips(7, now=FIX, db_path=db)
    assert len(t["trips"]) == 1
    trip = t["trips"][0]
    kwh = (80 - 75.9) / 100 * 91.2
    assert trip["miles"] == 12.0 and trip["kwh"] == round(kwh, 2) and trip["efficiency"] == round(12 / kwh, 2)
    assert trip["minutes"] == 20.0 and trip["avg_mph"] == 36.0 and trip["drive_mode"] == "Sport"
    assert trip["elevation_ft"] == 100 and trip["outside_f"] == 50.0 and trip["confidence"] == "good"
    assert trip["when"] == "Today 7:30 AM" and t["last"]["efficiency"] == trip["efficiency"]
    assert t["totals"]["efficiency"] == trip["efficiency"] and t["by_day"][FIX.date().isoformat()]["trips"] == 1
    d = t["drain"]
    # parked unplugged 07:52-09:00 (75.9 -> 75.8) and 09:02:20-17:00 (75.8 -> 75.0); 07:00-07:30 is under an hour
    assert d["days"][FIX.date().isoformat()]["drain_pct"] == pytest.approx(0.9, abs=0.01)
    assert d["parked_hours"] == pytest.approx(9.1, abs=0.1)
    assert d["pct_per_day"] == pytest.approx(0.9 / 9.09 * 24, abs=0.03)
    assert d["kwh_per_day"] == pytest.approx(d["pct_per_day"] / 100 * 91.2, abs=0.01)
    assert t["synced_ago"] == "just now"


def test_sync_rejects_bad_window(fake_ha):
    with pytest.raises(rivian.RivianError) as exc:
        rivian.sync(11)
    assert exc.value.code == 2


def test_short_trip_is_rough_and_temp_fit():
    t0 = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
    h = {"odo": rivian._Series([(t0, "10"), (t0 + timedelta(minutes=5), "12.5")]),
         "soc": rivian._Series([(t0, "50"), (t0 + timedelta(minutes=5), "49.0")]),
         "alt": rivian._Series([]), "mode": rivian._Series([], numeric=False), "temp": rivian._Series([])}
    trip = rivian._trip((t0, t0 + timedelta(minutes=6)), h, 90.0)
    assert trip["miles"] == 2.5 and trip["confidence"] == "rough" and trip["efficiency"] == round(2.5 / 0.9, 2)
    h["odo"] = rivian._Series([(t0, "10"), (t0 + timedelta(minutes=5), "10.3")])
    assert rivian._trip((t0, t0 + timedelta(minutes=6)), h, 90.0) is None  # garage shuffle
    assert rivian.temp_fit([(20, 2.0), (30, 2.4), (40, 2.8), (50, 3.2)]) is None  # too few
    fit = rivian.temp_fit([(20, 2.0), (30, 2.4), (40, 2.8), (50, 3.2), (60, 3.6)])
    assert fit["per_10f"] == 0.4 and fit["intercept"] == 1.2


def test_image_allow_list(fake_ha):
    assert rivian.image("three_quarter") == (b"PNG", "image/png")
    assert "/api/image_proxy/image.r2_three_quarter_dark" in fake_ha["calls"]
    with pytest.raises(rivian.RivianError):
        rivian.image("../../api/states")


def test_brief_headline(fake_ha):
    b = rivian.brief()
    assert b["headline"].startswith("54% · 163 mi · charging at 7.6 kW, to 80% ")
    assert b["headline"].endswith("· home") and b["charging"] and b["open"] == ["Rear left door"]


# ---- CLI ----

def test_cli_trips_and_sync(fake_ha, capsys):
    assert rivian_store.main(["trips", "--days", "7", "--no-sync"]) == 0
    assert json.loads(capsys.readouterr().out)["trips"] == []
    assert rivian_store.main(["sync", "--days", "0"]) == 2
    assert "days must be 1-10" in capsys.readouterr().err


def test_cli_brief_status_trends(fake_ha, capsys):
    assert rivian_store.main(["brief"]) == 0
    assert json.loads(capsys.readouterr().out)["soc"] == 54.1
    assert rivian_store.main(["status", "--section", "tires"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 4
    assert rivian_store.main(["trends", "--days", "9"]) == 2
    assert "days must be" in capsys.readouterr().err


def test_cli_unreachable_exits_1(capsys):
    assert rivian_store.main(["brief"]) == 1  # conftest keeps HA offline
    assert "Can't reach Home Assistant" in capsys.readouterr().err


# ---- dashboard ----

@pytest.fixture
def web(fake_ha, tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_STATE_DIR", str(tmp_path))
    from dashboard.app import app
    return TestClient(app)


def test_page_renders_and_matches_service(web):
    r = web.get("/home/ha/rivian")
    assert r.status_code == 200
    for text in ("Rivian R2", "163", "Charging", "The battery is cold, so charging is slowed.", "Rear left door open",
                 "Front right tire: low", "rv-diagram", "/home/ha/rivian/image/three_quarter"):
        assert text in r.text, text
    assert web.get("/home/ha/rivian?days=30").status_code == 200
    assert "No trips saved yet" in r.text
    api = web.get("/api/ha/rivian?days=7").json()
    svc = rivian.status()
    for key in ("energy", "closures", "tires", "health"):
        assert api["status"][key] == svc[key]
    assert api["trends"]["miles"] == rivian.trends(7)["miles"]


def test_page_shows_saved_trips(web, tmp_path):
    from dashboard.data import common
    rivian.sync(1, now=FIX, db_path=common.db_path())
    api = web.get("/api/ha/rivian?days=7").json()
    assert api["trips"] == rivian.trips(7, db_path=common.db_path())
    assert len(api["trips"]["trips"]) == 1  # the synthetic trip is at 07:30 today, always inside a 7-day window
    r = web.get("/home/ha/rivian")
    assert "Today 7:30 AM" in r.text and "rv-table" in r.text and "% per day" in r.text


def test_page_shows_error_when_ha_unreachable(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_STATE_DIR", str(tmp_path))
    from dashboard.app import app
    c = TestClient(app)
    r = c.get("/home/ha/rivian")
    assert r.status_code == 200 and "Can&#39;t reach Home Assistant" in r.text
    assert c.get("/api/ha/rivian").status_code == 503


def test_image_route(web):
    r = web.get("/home/ha/rivian/image/three_quarter")
    assert r.status_code == 200 and r.content == b"PNG" and r.headers["content-type"] == "image/png"
    assert web.get("/home/ha/rivian/image/nope").status_code == 404


def test_summary_tile(web):
    from dashboard.data import summary
    tile = next(t for t in summary.get_summary()["tiles"] if t["title"] == "Rivian")
    assert tile["headline"] == "54% · 163 mi" and tile["href"] == "/home/ha/rivian"
    assert "Rear left door open" in tile["sub"] and tile["state"] == "ok"
