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
# yesterday: 80 -> 70 -> 69 (11% of 91.2 kWh = 10.03 kWh used); a charging rise is ignored
HOURLY = {"sensor.r2_battery_state_of_charge": [
    {"start": _ms(1, 8), "mean": 80.0}, {"start": _ms(1, 9), "mean": 70.0}, {"start": _ms(1, 10), "mean": 69.0},
    {"start": _ms(1, 22), "mean": 75.0},
]}


@pytest.fixture
def fake_ha(monkeypatch):
    holder = {"states": base_states(), "calls": []}

    def fake_open(path, body=None, method=None, raw=False):
        holder["calls"].append(path)
        if path == "/api/states":
            return holder["states"]
        if path.startswith("/api/image_proxy/"):
            return b"PNG", "image/png"
        if path.startswith("/api/services/"):
            raise AssertionError("the Rivian service must never call an HA service")
        return []

    monkeypatch.setattr(ha, "_open", fake_open)
    monkeypatch.setattr(ha, "ws_commands", lambda cmds: [DAILY, HOURLY])
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


def test_trends_miles_energy_and_efficiency(fake_ha):
    t = rivian.trends(7)
    today = datetime.now(ha.local_tz()).date()
    y, d0 = (today - timedelta(days=1)).isoformat(), today.isoformat()
    assert len(t["dates"]) == 7 and t["dates"][-1] == d0
    assert t["miles"] == {y: 30.0, d0: 12.0}
    assert t["kwh_used"] == {y: round(11 / 100 * 91.2, 2)}
    assert t["efficiency"] == {y: round(30 / (11 / 100 * 91.2), 2)}
    assert t["soc"]["min"][y] == 60.0 and t["range_mi"][d0] == 171
    assert t["totals"]["miles_7d"] == 42.0 and t["data_days"] == 2 and t["limit"] == 80


def test_trends_rejects_bad_days(fake_ha):
    with pytest.raises(rivian.RivianError) as exc:
        rivian.trends(9)
    assert exc.value.code == 2


def test_efficiency_needs_enough_data():
    assert rivian.efficiency(2, 5) is None and rivian.efficiency(30, 0.5) is None
    assert rivian.efficiency(30, 10) == 3.0


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
    api = web.get("/api/ha/rivian?days=7").json()
    svc = rivian.status()
    for key in ("energy", "closures", "tires", "health"):
        assert api["status"][key] == svc[key]
    assert api["trends"]["miles"] == rivian.trends(7)["miles"]


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
