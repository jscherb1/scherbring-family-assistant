"""Home Assistant board + health: shared service rules, CLI, and UI/service parity (HA stubbed, no network)."""
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ha  # noqa: E402
import ha_store  # noqa: E402


def S(entity_id, state, **attrs):
    return {"entity_id": entity_id, "state": state, "attributes": attrs}


STATES = [
    S("climate.claire_thermostat", "off", current_temperature=71, temperature=68, hvac_modes=["heat", "off"],
      min_temp=60, max_temp=75, target_temp_step=1, friendly_name="Claire thermostat"),
    S("input_boolean.kid_door_alerts", "on", friendly_name="Kid Door Alerts"),
    S("alarm_control_panel.alarmo", "armed_home", friendly_name="Alarm"),
    S("cover.single_garage_door", "closed", friendly_name="Single"),
    S("cover.athom_garage_door", "open", friendly_name="Double"),
    S("binary_sensor.zb_doorsensor_frontdoor_opening", "on", device_class="opening", friendly_name="Front"),
    S("weather.openweathermap", "sunny", temperature=58, friendly_name="OWM"),
    S("sensor.a_battery", "12", device_class="battery", friendly_name="Remote battery"),
    S("sensor.b_battery", "88", device_class="battery", friendly_name="Phone battery"),
    S("sensor.gone", "unavailable", friendly_name="Gone sensor"),
    S("sensor.gone2", "unavailable", friendly_name="Gone sensor 2"),
    S("update.home_assistant_core_update", "on", friendly_name="Home Assistant Core Update",
      installed_version="2026.9.0", latest_version="2026.10.3"),
    S("update.mushroom_update", "on", friendly_name="Mushroom Update", installed_version="v5.0.5", latest_version="v5.2.3"),
    S("binary_sensor.backups_stale", "off", friendly_name="Backups Stale"),
    S("automation.old", "on", friendly_name="Old one", last_triggered="2025-01-01T00:00:00+00:00"),
    S("automation.fresh", "on", friendly_name="Fresh one", last_triggered="2026-10-24T00:00:00+00:00"),
    S("device_tracker.p1", "home", friendly_name="Justin’s iPhone"),
    S("device_tracker.p2", "home", friendly_name="Justin's iPhone"),
]
CONFIG = {"version": "2026.9.0", "state": "RUNNING", "location_name": "Rochester Home"}
WS = [
    [{"title": "Invalid config", "message": "- tapo_control could not be set up"},
     {"title": "Login attempt failed", "message": "invalid authentication from sonos (192.168.86.24)"}],
    {"issues": []},
    [{"entity_id": "sensor.gone", "platform": "tapo_control", "device_id": "d1"},
     {"entity_id": "sensor.gone2", "platform": "tapo_control", "device_id": "d1"}],
    [{"id": "d1", "name": "Basement Camera"}],
    {"views": [{"title": "Overview", "path": "o"}, {"title": "NoPath"}]},
]


@pytest.fixture
def fake_ha(monkeypatch):
    calls = []

    def fake_open(path, body=None, method=None, raw=False):
        calls.append((path, body))
        if path == "/api/states":
            return STATES
        if path == "/api/config":
            return CONFIG
        if path.startswith("/api/services/"):
            return []
        if path.startswith("/api/camera_proxy/"):
            return b"IMG", "image/jpeg"
        return []

    monkeypatch.setattr(ha, "_open", fake_open)
    monkeypatch.setattr(ha, "ws_commands", lambda cmds: WS)
    ha.clear_cache()
    return calls


def test_board_shapes_tiles(fake_ha):
    b = ha.board()
    assert b["strip"]["alarm"]["display"] == "Armed home"
    assert b["strip"]["weather"]["temperature"] == 58
    assert {g["name"]: g["display"] for g in b["strip"]["garages"]} == {"Single garage": "Closed", "Double garage": "Open"}
    door = next(d for d in b["security"]["doors"] if d["name"] == "Front door")
    assert door["display"] == "Open" and door["open"] is True
    claire = b["overview"]["thermostats"][0]
    assert claire["target"] == 68 and "set_temperature" in claire["writable"]
    assert [x["level"] for x in b["batteries"]] == [12, 88] and b["batteries"][0]["low"]


def test_board_skips_missing_entities(fake_ha):
    assert all(t["entity_id"] != "switch.christmas_tree" for t in ha.board()["holiday"])


def test_health_rules(fake_ha, tmp_path):
    h = ha.health(db_path=tmp_path / "t.db")
    titles = [a["title"] for a in h["attention"]]
    assert "Invalid config" in titles and "Login attempt failed" in titles
    assert "Remote battery at 12%" in titles and "Phone battery at 88%" not in titles
    assert "Double garage is open" in titles
    assert any("Core update" in t for t in titles)
    assert any("haven't run" in t for t in titles)
    assert h["attention"][0]["severity"] == "high"
    assert h["unavailable"]["by_integration"][0] == ("tapo_control", 2)
    recs = [r["title"] for r in h["recommendations"]]
    assert "Fix or remove the tapo_control integration" in recs
    assert "Find what is failing to log in" in recs
    assert "Duplicate device trackers" in recs
    assert any("no path" in r for r in recs)
    assert h["tiles"]["unavailable"] == 2 and h["tiles"]["updates"] == 2 and h["tiles"]["low_batteries"] == 1


def test_health_overnight_alarm_disarmed(fake_ha, tmp_path):
    from datetime import datetime
    STATES[2]["state"] = "disarmed"
    try:
        h = ha.health(db_path=tmp_path / "t.db", now=datetime(2026, 10, 24, 23, 0).astimezone())
    finally:
        STATES[2]["state"] = "armed_home"
    assert "Alarm is disarmed overnight" in [a["title"] for a in h["attention"]]


def test_health_run_history_is_throttled(fake_ha, tmp_path):
    db = tmp_path / "t.db"
    ha.health(db_path=db)
    ha.clear_cache()
    h = ha.health(db_path=db)
    assert len(h["trend"]) == 1  # second call inside the throttle window adds nothing


def test_act_allowlist_and_clamping(fake_ha):
    ha.act("climate.claire_thermostat", "set_temperature", "90")
    path, body = next(c for c in fake_ha if c[0].startswith("/api/services/"))
    assert path == "/api/services/climate/set_temperature" and body["temperature"] == 75  # clamped to max_temp
    ha.act("input_boolean.kid_door_alerts", "turn_off")
    assert fake_ha[-1][0] == "/api/states"
    assert any(c[0] == "/api/services/input_boolean/turn_off" for c in fake_ha)
    with pytest.raises(ha.HAError):
        ha.act("climate.claire_thermostat", "set_hvac_mode", "cool")  # not a supported mode
    for blocked in ("alarm_control_panel.alarmo", "cover.single_garage_door", "lock.front_door"):
        with pytest.raises(ha.HAError) as exc:
            ha.act(blocked, "turn_off")
        assert exc.value.code == 2
    with pytest.raises(ha.HAError):
        ha.act("input_boolean.kid_door_alerts", "set_temperature", "5")


def test_camera_only_for_board_cameras(fake_ha):
    assert ha.camera_image("camera.basement_hd_stream")[0] == b"IMG"
    with pytest.raises(ha.HAError):
        ha.camera_image("camera.somewhere_else")


def test_unreachable_is_a_typed_error():
    with pytest.raises(ha.HAError) as exc:
        ha.board()
    assert exc.value.code == 1


def test_cli_health_board_and_errors(fake_ha, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(ha, "DB_PATH", tmp_path / "cli.db")
    assert ha_store.main(["health", "--brief"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["tiles"]["updates"] == 2 and len(out["attention"]) <= 6
    assert ha_store.main(["board", "--section", "media"]) == 0
    assert ha_store.main(["board", "--section", "nope"]) == 2
    assert ha_store.main(["set-temp", "--entity", "climate.claire_thermostat", "--value", "70"]) == 0
    assert ha_store.main(["toggle", "--entity", "cover.single_garage_door", "--value", "on"]) == 2


def test_websocket_frames_decode_short_and_long_payloads():
    """The minimal client decodes unmasked server frames of every length class, incl. fragments and pings."""
    import socket as _s
    import struct
    import threading

    def frame(payload: bytes, opcode=1, fin=True):
        n = len(payload)
        head = bytes([(0x80 if fin else 0) | opcode])
        if n < 126:
            head += bytes([n])
        elif n < 65536:
            head += bytes([126]) + struct.pack(">H", n)
        else:
            head += bytes([127]) + struct.pack(">Q", n)
        return head + payload

    a, b = _s.socketpair()
    msgs = [{"id": 1, "blob": "x" * size} for size in (10, 300, 70000)]
    raw = b"".join(frame(json.dumps(m).encode()) for m in msgs)
    half = json.dumps({"id": 9}).encode()
    raw += frame(b"", opcode=9) + frame(half[:4], fin=False) + frame(half[4:], opcode=0)
    t = threading.Thread(target=lambda: a.sendall(raw)); t.start()
    assert [ha._ws_recv(b) for _ in range(3)] == msgs
    assert ha._ws_recv(b) == {"id": 9}
    t.join(); a.close(); b.close()


# ---- dashboard: pages, parity with the service, write route ----

@pytest.fixture
def web(fake_ha, tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_STATE_DIR", str(tmp_path))
    from dashboard.app import app
    return TestClient(app)


def test_pages_render_and_match_service(web, tmp_path):
    board = web.get("/home/ha")
    assert board.status_code == 200 and "Claire&#39;s Room" in board.text and "Armed home" in board.text
    assert web.get("/api/ha/board").json()["batteries"] == ha.board()["batteries"]
    health = web.get("/home/ha/health")
    assert health.status_code == 200 and "Fix or remove the tapo_control integration" in health.text
    api = web.get("/api/ha/health").json()
    assert api["tiles"]["updates"] == 2 and api["attention"] == ha.health(db_path=tmp_path / "x.db")["attention"]


def test_pages_show_error_when_ha_unreachable(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_STATE_DIR", str(tmp_path))
    from dashboard.app import app
    c = TestClient(app)
    for url in ("/home/ha", "/home/ha/health"):
        r = c.get(url)
        assert r.status_code == 200 and "Can&#39;t reach Home Assistant" in r.text
    assert "error" in c.get("/api/ha/board").json()


def test_act_route_same_origin_and_allowlist(web, fake_ha):
    r = web.post("/home/ha/act", data={"entity_id": "input_boolean.kid_door_alerts", "action": "turn_off"},
                 headers={"Origin": "http://evil.example"}, follow_redirects=False)
    assert r.status_code == 403
    assert not any(p.endswith("turn_off") for p, _ in fake_ha)
    r = web.post("/home/ha/act", data={"entity_id": "input_boolean.kid_door_alerts", "action": "turn_off"},
                 follow_redirects=False)
    assert r.status_code == 303 and any(p.endswith("input_boolean/turn_off") for p, _ in fake_ha)
    r = web.post("/home/ha/act", data={"entity_id": "cover.single_garage_door", "action": "turn_off"},
                 follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]


def test_output_is_escaped(web):
    STATES.append(S("sensor.evil_battery", "5", device_class="battery", friendly_name="<script>alert(1)</script>"))
    try:
        ha.clear_cache()
        assert "<script>alert(1)</script>" not in web.get("/home/ha").text
    finally:
        STATES.pop()


def test_camera_route(web):
    assert web.get("/home/ha/camera/camera.basement_hd_stream").content == b"IMG"
    assert web.get("/home/ha/camera/camera.nope").status_code == 404
