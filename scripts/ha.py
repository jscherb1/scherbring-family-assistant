"""Home Assistant board + health: the single shared service.

Zero-dependency (Python stdlib only). Talks to Home Assistant's REST API (and, for the registry,
notifications and repairs, a minimal websocket client) using HOMEASSISTANT_URL / HOMEASSISTANT_TOKEN
from paths.get_env. Both front ends are thin wrappers over this module:
  - scripts/ha_store.py   the CLI the `ha-overview` subagent runs
  - dashboard/routers/ha.py   the Family Board and HA Health pages and their /api/ha* twins

It holds three things in one place:
  - the board definition (which entities appear in which section of the replica dashboard)
  - the health rules (needs-attention list, recommendations)
  - the write allow-list (the only entities/actions the app may change)

Writes are deliberately narrow. Alarm, garage doors and locks are read-only: they are not in WRITABLE.
The fast lane (scripts/fastlane.py) has its own, separate list, FASTLANE_WRITABLE (porch lights, thermostat setpoint,
CLOSING a garage door), so the dashboard stays read-only for garage doors.
Failures raise HAError with a CLI-style code: 1 = not found / unreachable, 2 = bad input.
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import socket
import sqlite3
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import myhub
import paths

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "state" / "agent_results.db"
SCHEMA_PATH = REPO_ROOT / "state" / "schema.sql"

CACHE_SECONDS = 15
TIMEOUT = 8
LOW_BATTERY = 20
STALE_AUTOMATION_DAYS = 90
HEALTH_RUN_MIN_GAP_MINUTES = 10


class HAError(Exception):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.message = message
        self.code = code


# ---------------------------------------------------------------------------
# board definition (entity ids from the "Scherbring Family" HA dashboard)
# ---------------------------------------------------------------------------

THERMOSTATS = [
    ("climate.claire_thermostat", "Claire's Room"),
    ("climate.ruth_thermostat", "Ruth's Room"),
    ("climate.basement_bedroom_thermostat", "Basement Bedroom"),
    ("climate.entryway", "Entryway"),
    ("climate.basement_thermostat", "Basement"),
]
OVERVIEW_THERMOSTATS = THERMOSTATS[:3]
TEMP_SENSORS = [
    ("sensor.lumi_lumi_weather_temperature", "Claire's Room"),
    ("sensor.lumi_lumi_weather_temperature_2", "Ruth's Room"),
    ("sensor.zb_temp_masterbedroom_temperature", "Master Bedroom"),
    ("sensor.zb_temp_basement_temperature", "Basement"),
    ("sensor.entryway_temperature", "Entryway"),
    ("sensor.zb_basement_bedroom_temp_temperature", "Basement Bedroom"),
    ("sensor.zb_temp_garage_temperature", "Garage"),
    ("sensor.zb_temp_office_temperature", "Office"),
]
HEATER_SWITCHES = [
    ("input_boolean.ruth_s_space_heater_switch", "Ruth's space heater (auto)"),
    ("input_boolean.claire_s_space_heater_switch", "Claire's space heater (auto)"),
    ("switch.ruths_space_heater_power_switch", "Ruth's heater power"),
    ("switch.claires_space_heater_power_switch", "Claire's heater power"),
    ("switch.zb_outlet_basementheater_switch", "Basement heater power"),
]
HEATER_HISTORY = [
    ("switch.claires_space_heater_power_switch", "Claire"),
    ("switch.ruths_space_heater_power_switch", "Ruth"),
    ("switch.zb_outlet_basementheater_switch", "Basement"),
]
MEDIA = [
    ("media_player.basement_tv", "Basement TV"),
    ("media_player.living_room_tv", "Living Room TV"),
    ("media_player.living_room", "Living Room speaker"),
    ("media_player.kitchen_display_tv", "Kitchen Display TV"),
]
DOORS = [
    ("binary_sensor.zb_doorsensor_frontdoor_opening", "Front door"),
    ("binary_sensor.zb_doorsensor_garage_opening", "Garage door"),
    ("binary_sensor.zb_doorsensor_backdoor_opening", "Back door"),
    ("binary_sensor.lumi_lumi_sensor_magnet_aq2_opening", "Claire's door"),
    ("binary_sensor.zb_doorsensor_ruth_opening", "Ruth's door"),
    ("cover.single_garage_door", "Single garage"),
    ("cover.athom_garage_door", "Double garage"),
    ("binary_sensor.bed_presence_2b3d70_bed_occupied_either", "Bed presence"),
]
CAMERAS = [
    ("camera.tapo_c100_e09e_hd_stream", "Living Room"),
    ("camera.basement_hd_stream", "Basement"),
]
CALENDARS_FAMILY = [("calendar.scherbring_family", "Family"), ("calendar.kids_school_calendar", "School")]
CALENDAR_MEALS = ("calendar.meal_planning", "Meal plan")
PEOPLE = [("person.justin_scherbring", "Justin"), ("person.caroline_scherbring", "Caroline")]
IRRIGATION = [
    ("counter.irrigation_days_since_watered", "Days since watered"),
    ("switch.tz3000_okaz9tjs_ts011f", "Sprinkler"),
    ("input_number.pirate_weather_rain_chance_today", "Rain chance today"),
    ("input_number.pirate_weather_precip_amount_today", "Rain amount today"),
    ("sensor.24_hour_rain_amount", "Rain, 24 h"),
    ("sensor.48_hour_rain_amount", "Rain, 48 h"),
    ("sensor.72_hour_rain_amount", "Rain, 72 h"),
    ("sensor.openweathermap_rain", "Rain now"),
    ("input_boolean.irrigation_notification_acknowledged", "Notification acknowledged"),
    ("input_number.irrigation_frequency_days", "Water every (days)"),
]
HOLIDAY = [
    ("light.claires_light_light", "Claire's light"),
    ("switch.christmas_tree", "Christmas tree"),
    ("switch.christmas_lights", "Front Christmas lights"),
    ("switch.zb_outlet_backyard_christmas_switch", "Backyard Christmas lights"),
    ("switch.zb_outlet_christmas_garland", "Christmas garland"),
    ("automation.turn_off_christmas_tree_lights", "Turn off Christmas lights"),
    ("automation.turn_on_christmas_lights", "Turn on Christmas lights"),
]
KID_DOOR_ALERTS = "input_boolean.kid_door_alerts"
ALARM = "alarm_control_panel.alarmo"
GARAGE_CHIPS = [("cover.single_garage_door", "Single garage"), ("cover.athom_garage_door", "Double garage")]
WEATHER_NOW = "weather.openweathermap"
WEATHER_FORECAST = "weather.pirateweather"

# Write allow-list: entity -> actions. Anything not here (alarm, garage doors, locks) is read-only.
_ON_OFF = {"turn_on", "turn_off"}
WRITABLE: dict[str, set[str]] = {
    **{eid: {"set_temperature", "set_hvac_mode"} for eid, _ in THERMOSTATS},
    KID_DOOR_ALERTS: set(_ON_OFF),
    **{eid: set(_ON_OFF) for eid, _ in HOLIDAY if eid.split(".")[0] in ("switch", "automation")},
    **{eid: {"media_play_pause", "volume_up", "volume_down"} for eid, _ in MEDIA},
}
_SERVICE = {
    "set_temperature": ("climate", "set_temperature"),
    "set_hvac_mode": ("climate", "set_hvac_mode"),
    "media_play_pause": ("media_player", "media_play_pause"),
    "volume_up": ("media_player", "volume_up"),
    "volume_down": ("media_player", "volume_down"),
}

# Fast-lane allow-list (scripts/fastlane.py): a SEPARATE, narrower list than WRITABLE so the dashboard stays
# read-only for garage doors. The fast lane may switch the porch lights, set a thermostat, and CLOSE the two garage
# doors. It can never open a garage, unlock, or touch the alarm: those are not here, so `fastlane_act` refuses them
# and such requests go to the full agent. Default-deny: any entity not listed is refused.
FASTLANE_PORCH = "switch.shelly_relay_frontoutdoorlights_switch_0"
FASTLANE_GARAGES = ("cover.single_garage_door", "cover.athom_garage_door")
FASTLANE_WRITABLE: dict[str, set[str]] = {
    FASTLANE_PORCH: set(_ON_OFF),
    **{eid: {"set_temperature"} for eid, _ in THERMOSTATS},
    **{eid: {"close_cover"} for eid in FASTLANE_GARAGES},
}
_SERVICE["close_cover"] = ("cover", "close_cover")  # only reachable through FASTLANE_WRITABLE

# Spoken names -> entity ids (lowercase, apostrophes removed, the words "the/my/our/thermostat/temperature" dropped).
FASTLANE_ALIASES: dict[str, tuple[str, ...]] = {
    "porch lights": (FASTLANE_PORCH,), "porch light": (FASTLANE_PORCH,),
    "front porch lights": (FASTLANE_PORCH,), "front porch light": (FASTLANE_PORCH,),
    "outside lights": (FASTLANE_PORCH,), "outside light": (FASTLANE_PORCH,),
    "outdoor lights": (FASTLANE_PORCH,), "outdoor light": (FASTLANE_PORCH,),
    "front door lights": (FASTLANE_PORCH,), "front door light": (FASTLANE_PORCH,),
    "front outdoor lights": (FASTLANE_PORCH,), "front lights": (FASTLANE_PORCH,),
    "claires room": ("climate.claire_thermostat",), "claire room": ("climate.claire_thermostat",),
    "claire": ("climate.claire_thermostat",),
    "ruths room": ("climate.ruth_thermostat",), "ruth room": ("climate.ruth_thermostat",),
    "ruth": ("climate.ruth_thermostat",),
    "basement bedroom": ("climate.basement_bedroom_thermostat",),
    "entryway": ("climate.entryway",), "entry": ("climate.entryway",),
    "basement": ("climate.basement_thermostat",),
    "garage": FASTLANE_GARAGES, "garage door": FASTLANE_GARAGES, "garage doors": FASTLANE_GARAGES,
    "both garage doors": FASTLANE_GARAGES, "both garages": FASTLANE_GARAGES,
    "single garage": ("cover.single_garage_door",), "single garage door": ("cover.single_garage_door",),
    "single door": ("cover.single_garage_door",),
    "double garage": ("cover.athom_garage_door",), "double garage door": ("cover.athom_garage_door",),
    "double door": ("cover.athom_garage_door",),
}
# Read-only questions the fast lane answers (state only, never a write).
FASTLANE_READ_ALIASES: dict[str, tuple[str, ...]] = {
    "front door": ("binary_sensor.zb_doorsensor_frontdoor_opening",),
    "back door": ("binary_sensor.zb_doorsensor_backdoor_opening",),
    "claires door": ("binary_sensor.lumi_lumi_sensor_magnet_aq2_opening",),
    "ruths door": ("binary_sensor.zb_doorsensor_ruth_opening",),
    **{k: v for k, v in FASTLANE_ALIASES.items() if k.startswith(("garage", "single", "double", "both"))},
    "porch lights": (FASTLANE_PORCH,), "porch light": (FASTLANE_PORCH,),
    "outside lights": (FASTLANE_PORCH,), "front door lights": (FASTLANE_PORCH,),
}

OPENING_CLASSES = ("opening", "door", "garage_door", "window")

CONDITION_ICON = {
    "sunny": "☀️", "clear-night": "🌙", "partlycloudy": "⛅", "cloudy": "☁️", "rainy": "🌧️", "pouring": "🌧️",
    "snowy": "❄️", "snowy-rainy": "🌨️", "fog": "🌫️", "windy": "💨", "lightning": "⚡", "lightning-rainy": "⛈️",
    "hail": "🌨️", "exceptional": "❗",
}


# ---------------------------------------------------------------------------
# HA client (REST + minimal websocket), with a short cache
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[float, object]] = {}


def clear_cache() -> None:
    _cache.clear()


def _creds() -> tuple[str, str]:
    url = paths.get_env("HOMEASSISTANT_URL")
    token = paths.get_env("HOMEASSISTANT_TOKEN")
    if not url or not token:
        raise HAError("Home Assistant isn't configured (HOMEASSISTANT_URL / HOMEASSISTANT_TOKEN).", 1)
    return url.rstrip("/"), token


def _open(path: str, body=None, method: str | None = None, raw: bool = False):
    url, token = _creds()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url + path, data=data, method=method or ("POST" if data is not None else "GET"),
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = resp.read()
            return (payload, resp.headers.get("Content-Type", "")) if raw else json.loads(payload or b"null")
    except urllib.error.HTTPError as exc:
        raise HAError(f"Home Assistant returned HTTP {exc.code} for {path.split('?')[0]}", 1) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise HAError(f"Can't reach Home Assistant ({getattr(exc, 'reason', exc)})", 1) from None


def _get(path: str, ttl: int = CACHE_SECONDS):
    hit = _cache.get(path)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    value = _open(path)
    _cache[path] = (time.monotonic(), value)
    return value


def states() -> dict[str, dict]:
    return {s["entity_id"]: s for s in _get("/api/states")}


def _read_exact(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("connection closed")
        buf += chunk
    return buf


def _ws_recv(sock) -> dict:
    message = b""
    while True:
        b1, b2 = _read_exact(sock, 2)
        opcode, length = b1 & 0x0F, b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", _read_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack(">Q", _read_exact(sock, 8))[0]
        payload = _read_exact(sock, length)
        if opcode == 8:
            raise OSError("websocket closed")
        if opcode in (9, 10):
            continue
        message += payload
        if b1 & 0x80:
            return json.loads(message)


def _ws_send(sock, obj: dict) -> None:
    payload = json.dumps(obj).encode()
    header = bytes([0x81])
    n = len(payload)
    if n < 126:
        header += bytes([0x80 | n])
    elif n < 65536:
        header += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        header += bytes([0x80 | 127]) + struct.pack(">Q", n)
    mask = os.urandom(4)
    sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def ws_commands(commands: list[dict]) -> list[dict | None]:
    """Run websocket commands in one connection. Returns each result (None if that command failed)."""
    url, token = _creds()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https":
        raise HAError("https Home Assistant URLs aren't supported by the websocket client", 2)
    try:
        sock = socket.create_connection((parsed.hostname, parsed.port or 80), timeout=TIMEOUT)
        sock.settimeout(TIMEOUT + 10)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET /api/websocket HTTP/1.1\r\nHost: {parsed.netloc}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += _read_exact(sock, 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise OSError("websocket upgrade refused")
        _ws_recv(sock)  # auth_required
        _ws_send(sock, {"type": "auth", "access_token": token})
        if _ws_recv(sock).get("type") != "auth_ok":
            raise HAError("Home Assistant rejected the access token", 1)
        results: list[dict | None] = []
        for i, cmd in enumerate(commands, start=1):
            _ws_send(sock, {"id": i, **cmd})
            while True:
                msg = _ws_recv(sock)
                if msg.get("id") == i and msg.get("type") == "result":
                    results.append(msg.get("result") if msg.get("success") else None)
                    break
        sock.close()
        return results
    except HAError:
        raise
    except (OSError, ValueError) as exc:
        raise HAError(f"Can't reach Home Assistant websocket ({exc})", 1) from None


def _ws_cached(name: str, commands: list[dict]):
    hit = _cache.get("ws:" + name)
    if hit and time.monotonic() - hit[0] < 60:
        return hit[1]
    value = ws_commands(commands)
    _cache["ws:" + name] = (time.monotonic(), value)
    return value


# ---------------------------------------------------------------------------
# tiles
# ---------------------------------------------------------------------------


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _display(st: dict) -> str:
    state = st["state"]
    unit = st["attributes"].get("unit_of_measurement", "")
    n = _num(state)
    if n is not None and st["entity_id"].split(".")[0] in ("sensor", "input_number", "number"):
        text = f"{n:.1f}".rstrip("0").rstrip(".") if n != int(n) else str(int(n))
        return f"{text} {unit}".strip()
    return state.replace("_", " ").capitalize()


DIAL_R = 50
DIAL_SWEEP = 270


def _dial(current, target, lo, hi) -> dict | None:
    """Geometry for a 270-degree ring gauge (viewBox 0 0 120 120, centre 60,60): the arc is filled up to the
    current temperature and a dot marks the setpoint."""
    lo, hi = _num(lo), _num(hi)
    if lo is None or hi is None or hi <= lo:
        return None
    circ = 2 * math.pi * DIAL_R
    frac = lambda v: max(0.0, min(1.0, (float(v) - lo) / (hi - lo)))  # noqa: E731
    out = {"circ": round(circ, 2), "track": round(circ * DIAL_SWEEP / 360, 2),
           "fill": round(circ * DIAL_SWEEP / 360 * frac(current), 2) if _num(current) is not None else 0, "marker": None}
    if _num(target) is not None:
        angle = math.radians(135 + DIAL_SWEEP * frac(target))
        out["marker"] = {"x": round(60 + DIAL_R * math.cos(angle), 1), "y": round(60 + DIAL_R * math.sin(angle), 1)}
    return out


def _tile(all_states: dict, entity_id: str, name: str | None = None) -> dict | None:
    st = all_states.get(entity_id)
    if st is None:
        return None
    attrs = st["attributes"]
    available = st["state"] not in ("unavailable", "unknown")
    tile = {
        "entity_id": entity_id,
        "name": name or attrs.get("friendly_name") or entity_id,
        "state": st["state"],
        "display": _display(st),
        "available": available,
        "writable": sorted(WRITABLE.get(entity_id, ())),
    }
    domain = entity_id.split(".")[0]
    if domain == "climate":
        tile.update(
            current=attrs.get("current_temperature"), target=attrs.get("temperature"),
            hvac_modes=attrs.get("hvac_modes", []), min_temp=attrs.get("min_temp"), max_temp=attrs.get("max_temp"),
            step=attrs.get("target_temp_step") or 1, display=st["state"].replace("_", " ").capitalize(),
            heating=attrs.get("hvac_action") == "heating" or st["state"] == "heat",
            dial=_dial(attrs.get("current_temperature"), attrs.get("temperature"), attrs.get("min_temp"), attrs.get("max_temp")))
    elif domain == "media_player":
        vol = attrs.get("volume_level")
        tile.update(title=attrs.get("media_title") or "", volume=None if vol is None else round(vol * 100))
    elif domain in ("binary_sensor", "cover"):
        tile["open"] = st["state"] in ("on", "open", "opening")
        if available and domain == "binary_sensor" and attrs.get("device_class") in OPENING_CLASSES:
            tile["display"] = "Open" if tile["open"] else "Closed"
    elif domain == "person":
        tile["home"] = st["state"] == "home"
        tile["display"] = "Home" if st["state"] == "home" else st["state"].replace("_", " ").title()
    return tile


def _tiles(all_states: dict, pairs) -> list[dict]:
    return [t for eid, name in pairs if (t := _tile(all_states, eid, name))]


# ---------------------------------------------------------------------------
# board
# ---------------------------------------------------------------------------


def _calendar_events(entity_id: str, days: int, limit: int = 12) -> list[dict]:
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    q = urllib.parse.urlencode({"start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "end": (start + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")})
    try:
        raw = _get(f"/api/calendars/{entity_id}?{q}", ttl=120)
    except HAError:
        return []
    events = []
    for e in raw:
        s = e.get("start", {})
        start = s.get("dateTime") or s.get("date") or ""
        events.append({"title": e.get("summary", ""), "all_day": "date" in s and "dateTime" not in s,
                       "start": start, "day": start[:10], "calendar": entity_id})
    events.sort(key=lambda e: e["start"])
    return events[:limit]


LOCAL_TZ_NAME = "America/Chicago"


def local_tz():
    from zoneinfo import ZoneInfo
    return ZoneInfo(LOCAL_TZ_NAME)


def list_calendars() -> list[dict]:
    """Every calendar HA exposes: [{"entity_id", "name"}]. Raises HAError when HA can't be read."""
    raw = _get("/api/calendars", ttl=0)
    return [{"entity_id": c["entity_id"], "name": c.get("name") or c["entity_id"]} for c in raw or []]


def calendar_window(entity_id: str, first_day, last_day) -> list[dict]:
    """Events of one calendar for the LOCAL (America/Chicago) days first_day..last_day inclusive.

    Unlike `_calendar_events` (UTC midnight, no end time, errors swallowed) this uses local day boundaries,
    returns end times and raises HAError on failure so a caller can tell "no events" from "couldn't read".
    Returns [{title, day, start_time, end_time, all_day, calendar}] sorted by day and time. Timed events sit
    on their local start day. All-day events appear on EACH day they cover (HA's all-day end is exclusive),
    clipped to the window, with start_time/end_time None."""
    tz = local_tz()
    lo = datetime(first_day.year, first_day.month, first_day.day, tzinfo=tz)
    nxt = last_day + timedelta(days=1)
    hi = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    q = urllib.parse.urlencode({"start": lo.astimezone(timezone.utc).strftime(fmt),
                                "end": hi.astimezone(timezone.utc).strftime(fmt)})
    raw = _get(f"/api/calendars/{entity_id}?{q}", ttl=0)
    out = []
    for e in raw or []:
        s, en = e.get("start") or {}, e.get("end") or {}
        title = e.get("summary", "")
        if s.get("dateTime"):
            st = datetime.fromisoformat(s["dateTime"].replace("Z", "+00:00"))
            st = st.astimezone(tz) if st.tzinfo else st.replace(tzinfo=tz)
            day = st.date()
            if not first_day <= day <= last_day:
                continue
            end_t = None
            if en.get("dateTime"):
                et = datetime.fromisoformat(en["dateTime"].replace("Z", "+00:00"))
                et = et.astimezone(tz) if et.tzinfo else et.replace(tzinfo=tz)
                end_t = et.strftime("%H:%M")
            out.append({"title": title, "day": day.isoformat(), "start_time": st.strftime("%H:%M"),
                        "end_time": end_t, "all_day": False, "calendar": entity_id})
        elif s.get("date"):
            d0 = datetime.strptime(s["date"], "%Y-%m-%d").date()
            d1 = datetime.strptime(en["date"], "%Y-%m-%d").date() if en.get("date") else d0 + timedelta(days=1)
            if d1 <= d0:
                d1 = d0 + timedelta(days=1)
            d = d0
            while d < d1:
                if first_day <= d <= last_day:
                    out.append({"title": title, "day": d.isoformat(), "start_time": None, "end_time": None,
                                "all_day": True, "calendar": entity_id})
                d += timedelta(days=1)
    out.sort(key=lambda x: (x["day"], x["start_time"] or "", x["title"]))
    return out


def _forecast() -> list[dict]:
    try:
        resp = _open("/api/services/weather/get_forecasts?return_response",
                     {"entity_id": WEATHER_FORECAST, "type": "daily"})
        days = resp["service_response"][WEATHER_FORECAST]["forecast"][:5]
    except (HAError, KeyError, TypeError):
        return []
    return [{"date": d.get("datetime", "")[:10], "high": d.get("temperature"), "low": d.get("templow"),
             "condition": d.get("condition", ""), "icon": CONDITION_ICON.get(d.get("condition", ""), ""),
             "rain_chance": d.get("precipitation_probability")} for d in days]


def _heater_hours() -> list[dict]:
    """Hours each heater was on in the last 24 h."""
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ids = ",".join(e for e, _ in HEATER_HISTORY)
    try:
        rows = _get(f"/api/history/period/{since}?filter_entity_id={ids}&minimal_response&no_attributes", ttl=120)
    except HAError:
        return []
    out = []
    now = datetime.now(timezone.utc)
    for series, (_, name) in zip(rows, HEATER_HISTORY):
        minutes, prev = 0.0, None
        for point in series:
            ts = point.get("last_changed") or point.get("last_updated")
            when = datetime.fromisoformat(ts) if ts else None
            if prev and prev[1] == "on" and when:
                minutes += (when - prev[0]).total_seconds() / 60
            if when:
                prev = (when, point["state"])
        if prev and prev[1] == "on":
            minutes += (now - prev[0]).total_seconds() / 60
        out.append({"name": name, "minutes_on": round(minutes)})
    return out


def _batteries(all_states: dict) -> list[dict]:
    rows = []
    for eid, st in all_states.items():
        if eid.startswith("sensor.") and st["attributes"].get("device_class") == "battery":
            level = _num(st["state"])
            if level is not None:
                rows.append({"entity_id": eid, "name": st["attributes"].get("friendly_name", eid),
                             "level": round(level), "low": level < LOW_BATTERY})
    return sorted(rows, key=lambda r: r["level"])


def board() -> dict:
    """The replica of the Scherbring Family HA dashboard, one dict per section."""
    st = states()
    weather = _tile(st, WEATHER_NOW)
    if weather:
        weather["temperature"] = st[WEATHER_NOW]["attributes"].get("temperature")
        weather["icon"] = CONDITION_ICON.get(st[WEATHER_NOW]["state"], "")
    alarm = _tile(st, ALARM)
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "strip": {
            "alarm": alarm, "weather": weather, "garages": _tiles(st, GARAGE_CHIPS),
            "kid_door_alerts": _tile(st, KID_DOOR_ALERTS, "Kid door alerts"),
            "people": _tiles(st, PEOPLE),
            "low_batteries": sum(1 for b in _batteries(st) if b["low"]),
            "updates": sum(1 for e, x in st.items() if e.startswith("update.") and x["state"] == "on"),
        },
        "overview": {
            "forecast": _forecast(),
            "thermostats": _tiles(st, OVERVIEW_THERMOSTATS),
            "meals": _calendar_events(CALENDAR_MEALS[0], 5),
            "calendar": _merge_events([_calendar_events(e, 7) for e, _ in CALENDARS_FAMILY]),
            "people": _tiles(st, PEOPLE),
        },
        "climate": {
            "temperatures": _tiles(st, TEMP_SENSORS), "thermostats": _tiles(st, THERMOSTATS),
            "heaters": _tiles(st, HEATER_SWITCHES), "heater_hours": _heater_hours(),
        },
        "media": _tiles(st, MEDIA),
        "security": {"alarm": alarm, "doors": _tiles(st, DOORS), "cameras": _tiles(st, CAMERAS)},
        "calendar": {
            "family": _merge_events([_calendar_events(e, 30, 40) for e, _ in CALENDARS_FAMILY]),
            "meals": _calendar_events(CALENDAR_MEALS[0], 14, 20),
        },
        "batteries": _batteries(st),
        "irrigation": _tiles(st, IRRIGATION),
        "holiday": _tiles(st, HOLIDAY),
    }


def _merge_events(lists: list[list[dict]]) -> list[dict]:
    return sorted((e for lst in lists for e in lst), key=lambda e: e["start"])[:20]


# ---------------------------------------------------------------------------
# writes (allow-listed)
# ---------------------------------------------------------------------------


def act(entity_id: str, action: str, value=None) -> dict:
    """Change one allow-listed entity (the dashboard's WRITABLE list). Returns the fresh tile."""
    return _apply(entity_id, action, value, WRITABLE)


def fastlane_act(entity_id: str, action: str, value=None) -> dict:
    """Change one entity on the FAST-LANE allow-list (porch lights, thermostat setpoint, close a garage door).
    Anything else, including opening a garage, locks and the alarm, is refused with HAError code 2."""
    return _apply(entity_id, action, value, FASTLANE_WRITABLE)


def fastlane_read(entity_id: str) -> dict:
    """Fresh single-entity state for the fast lane's read questions (bypasses the 15 s cache)."""
    known = {e for v in FASTLANE_READ_ALIASES.values() for e in v} | {e for e, _ in THERMOSTATS} \
        | {e for e, _ in TEMP_SENSORS}
    if entity_id not in known:
        raise HAError(f"{entity_id} isn't readable from the fast lane", 2)
    st = _open("/api/states/" + urllib.parse.quote(entity_id, safe="._"))
    if not isinstance(st, dict) or "state" not in st:
        raise HAError(f"no such entity {entity_id}", 1)
    return _tile({entity_id: st}, entity_id)


def _apply(entity_id: str, action: str, value, allow: dict[str, set[str]]) -> dict:
    allowed = allow.get(entity_id)
    if allowed is None:
        raise HAError(f"{entity_id} can't be changed from the app (read-only)", 2)
    if action not in allowed:
        raise HAError(f"{action!r} isn't allowed for {entity_id}", 2)
    st = states().get(entity_id)
    if st is None:
        raise HAError(f"no such entity {entity_id}", 1)
    domain = entity_id.split(".")[0]
    body: dict = {"entity_id": entity_id}
    if action in _ON_OFF:
        domain_, service = domain, action
    else:
        domain_, service = _SERVICE[action]
    if action == "set_temperature":
        temp = _num(value)
        if temp is None:
            raise HAError("temperature must be a number", 2)
        lo, hi = st["attributes"].get("min_temp"), st["attributes"].get("max_temp")
        if lo is not None and hi is not None:
            temp = max(lo, min(hi, temp))
        body["temperature"] = temp
    elif action == "set_hvac_mode":
        if value not in st["attributes"].get("hvac_modes", []):
            raise HAError(f"{value!r} isn't a mode for {entity_id}", 2)
        body["hvac_mode"] = value
    _open(f"/api/services/{domain_}/{service}", body)
    clear_cache()
    fresh = states().get(entity_id)
    return _tile({entity_id: fresh}, entity_id) if fresh else {"entity_id": entity_id}


def camera_image(entity_id: str) -> tuple[bytes, str]:
    if entity_id not in {e for e, _ in CAMERAS}:
        raise HAError("not a board camera", 2)
    return _open(f"/api/camera_proxy/{entity_id}", raw=True)


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _parse_ts(value) -> datetime | None:
    if not value or value in ("unknown", "unavailable"):
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _norm_name(name: str) -> str:
    return re.sub(r"[’‘`]", "'", name).strip().lower()


def _update_category(eid: str, st: dict) -> str:
    if eid == "update.home_assistant_core_update":
        return "Core"
    if eid == "update.home_assistant_operating_system_update":
        return "OS"
    if "supervisor" in eid:
        return "Supervisor"
    name = st["attributes"].get("friendly_name", "").lower()
    if "firmware" in name:
        return "Device firmware"
    if str(st["attributes"].get("installed_version", "")).startswith("v"):
        return "HACS cards & integrations"
    return "Add-ons"


def _updates(all_states: dict) -> list[dict]:
    rows = []
    for eid, st in all_states.items():
        if eid.startswith("update.") and st["state"] == "on":
            a = st["attributes"]
            rows.append({"entity_id": eid, "name": a.get("friendly_name", eid).replace(" Update", ""),
                         "installed": a.get("installed_version"), "latest": a.get("latest_version"),
                         "category": _update_category(eid, st)})
    return rows


def _unavailable(all_states: dict, registry: list | None, device_registry: list | None) -> dict:
    platform = {e["entity_id"]: e.get("platform") for e in (registry or [])}
    device_name = {}
    for e in registry or []:
        device_name[e["entity_id"]] = e.get("device_id")
    devices = {d["id"]: (d.get("name_by_user") or d.get("name") or "") for d in (device_registry or [])}
    items = [eid for eid, st in all_states.items() if st["state"] == "unavailable"]
    by_integration: dict[str, int] = {}
    by_device: dict[str, int] = {}
    for eid in items:
        by_integration[platform.get(eid) or "unknown"] = by_integration.get(platform.get(eid) or "unknown", 0) + 1
        dev = devices.get(device_name.get(eid) or "", "") or all_states[eid]["attributes"].get("friendly_name", eid)
        by_device[dev] = by_device.get(dev, 0) + 1
    top = lambda d, n: sorted(d.items(), key=lambda kv: -kv[1])[:n]  # noqa: E731
    return {"count": len(items), "by_integration": top(by_integration, 8), "by_device": top(by_device, 8)}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def _add(items: list, severity: str, category: str, title: str, detail: str = "", key: str = "") -> None:
    """key is stable across refreshes (counts and versions stay out of it) so a dismissal sticks."""
    items.append({"severity": severity, "category": category, "title": title, "detail": detail,
                  "key": "ha:" + (key or f"{_slug(category)}:{_slug(title)}")})


def health(db_path: Path | str | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now().astimezone()
    st = states()
    config = _get("/api/config", ttl=300)
    ws = _ws_cached("health", [{"type": "persistent_notification/get"}, {"type": "repairs/list_issues"},
                               {"type": "config/entity_registry/list"}, {"type": "config/device_registry/list"},
                               {"type": "lovelace/config", "url_path": "scherbring-family"}])
    notifications, repairs, ent_reg, dev_reg, lovelace = (list(ws) + [None] * 5)[:5]
    notifications = notifications or []
    issues = [i for i in (repairs or {}).get("issues", []) if not i.get("ignored")]

    attention: list[dict] = []
    recs: list[dict] = []

    for n in notifications:
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", n.get("message", "")).replace("\n", " ").strip()
        sev = "medium" if "Login attempt" in n.get("title", "") else "high"
        _add(attention, sev, "Notification", n.get("title", "HA notification"), text[:240],
             key=f"notification:{n.get('notification_id') or _slug(n.get('title', ''))}")
    for i in issues:
        _add(attention, "high" if i.get("severity") in ("error", "critical") else "medium", "Repair",
             i.get("translation_key") or i.get("issue_id", "Repair"), i.get("domain", ""),
             key=f"repair:{i.get('domain', '')}:{i.get('issue_id', '')}")

    unavailable = _unavailable(st, ent_reg, dev_reg)
    total_entities = len(st)
    if unavailable["count"]:
        top = ", ".join(f"{name} ({n})" for name, n in unavailable["by_device"][:4])
        _add(attention, "medium", "Unavailable", f"{unavailable['count']} of {total_entities} entities unavailable",
             f"Biggest: {top}", key="unavailable")

    batteries = _batteries(st)
    for b in [b for b in batteries if b["low"]]:
        _add(attention, "medium", "Battery", f"{b['name']} at {b['level']}%", key=f"battery:{b['entity_id']}")

    alarm = st.get(ALARM)
    for eid, name in DOORS:
        s = st.get(eid)
        if s and s["state"] in ("on", "open") and "garage" in eid:
            _add(attention, "high", "Door", f"{name} is open", key=f"door:{eid}")
    if alarm and alarm["state"] == "disarmed" and (now.hour >= 22 or now.hour < 6):
        _add(attention, "high", "Alarm", "Alarm is disarmed overnight", key="alarm-overnight")

    for eid, name in [(ALARM, "Alarm panel")] + GARAGE_CHIPS + THERMOSTATS:
        x = st.get(eid)
        if x and x["state"] == "unavailable":
            _add(attention, "high", "Critical device", f"{name} is unavailable",
                 "The app can't see or control it; check its integration or power.", key=f"critical:{eid}")

    updates = _updates(st)
    by_cat: dict[str, list] = {}
    for u in updates:
        by_cat.setdefault(u["category"], []).append(u)
    for cat, rows in by_cat.items():
        sev = "medium" if cat in ("Core", "OS", "Supervisor") else "low"
        names = ", ".join(r["name"] for r in rows[:4]) + (f" +{len(rows) - 4} more" if len(rows) > 4 else "")
        _add(attention, sev, "Updates", f"{len(rows)} {cat} update{'s' if len(rows) != 1 else ''} available", names,
             key=f"updates:{_slug(cat)}")

    stale = st.get("binary_sensor.backups_stale")
    if stale and stale["state"] == "on":
        _add(attention, "high", "Backup", "Backups are stale", "Home Assistant reports no recent successful backup", key="backup")
    elif not stale:
        _add(attention, "low", "Backup", "No backup status sensor found", key="backup-sensor")

    automations = [(eid, s) for eid, s in st.items() if eid.startswith("automation.")]
    stale_autos = []
    for eid, s in automations:
        if s["state"] != "on":
            continue
        ts = _parse_ts(s["attributes"].get("last_triggered"))
        if ts is None or now - ts > timedelta(days=STALE_AUTOMATION_DAYS):
            stale_autos.append(s["attributes"].get("friendly_name", eid))
    if stale_autos:
        _add(attention, "low", "Automations", f"{len(stale_autos)} enabled automations haven't run in {STALE_AUTOMATION_DAYS}+ days",
             ", ".join(stale_autos[:5]), key="stale-automations")

    attention.sort(key=lambda a: SEVERITY_ORDER[a["severity"]])

    # --- recommendations (derived from the same data) ---
    for name, n in unavailable["by_integration"]:
        related = [x for x in notifications if name in x.get("message", "")]
        if related and name != "unknown":
            _add(recs, "high", "Cleanup", f"Fix or remove the {name} integration",
                 f"{n} of its entities are unavailable and HA reports it failed to set up.", key=f"rec:integration:{name}")
    if unavailable["count"] > 50:
        tops = ", ".join(f"{name} ({n})" for name, n in unavailable["by_integration"][:3])
        _add(recs, "medium", "Cleanup", f"Bulk-clean the {unavailable['count']} unavailable entities",
             f"By integration: {tops}. Delete leftovers from old devices so real outages stand out.", key="rec:bulk-clean")
    for n in notifications:
        if "Login attempt" in n.get("title", ""):
            who = re.search(r"\(([\d.]+)\)", n.get("message", ""))
            _add(recs, "medium", "Security", "Find what is failing to log in",
                 f"Something{' at ' + who.group(1) if who else ''} is sending bad credentials to Home Assistant; "
                 "usually an integration with a stale token.", key="rec:login-failures")
    if updates:
        core = by_cat.get("Core")
        _add(recs, "medium" if core else "low", "Updates", f"Apply the {len(updates)} pending updates",
             ("Core is behind (" + f"{core[0]['installed']} → {core[0]['latest']}" + "). " if core else "")
             + "Back up first, then update in batches: add-ons, then HACS cards, then Core.", key="rec:updates")
    trackers = [(eid, s["attributes"].get("friendly_name", "")) for eid, s in st.items() if eid.startswith("device_tracker.")]
    seen: dict[str, int] = {}
    for _, name in trackers:
        seen[_norm_name(name)] = seen.get(_norm_name(name), 0) + 1
    dupes = [n for n, c in seen.items() if c > 1]
    if dupes:
        _add(recs, "low", "Cleanup", "Duplicate device trackers", ", ".join(dupes) + " each appear more than once.", key="rec:duplicate-trackers")
    views = [v for v in (lovelace or {}).get("views", []) if not v.get("path")]
    if views:
        _add(recs, "low", "Dashboard", f"{len(views)} dashboard view(s) in the Scherbring Family dashboard have no path",
             "They get fragile numeric URLs; give them a path in the dashboard editor.", key="rec:view-paths")
    if not any("battery" in s["attributes"].get("friendly_name", "").lower() for _, s in automations):
        _add(recs, "low", "Idea", "Add a low-battery notification automation",
             f"{sum(1 for b in batteries if b['low'])} sensors are below {LOW_BATTERY}% today and nothing alerts you.", key="rec:battery-automation")
    _add(recs, "low", "Idea", "Add an 'HA unreachable' alert from the family app",
         "This page can tell when HA stops answering; a scheduled check could message you.", key="rec:unreachable-alert")
    recs.sort(key=lambda a: SEVERITY_ORDER[a["severity"]])
    gone = _dismissed(db_path)
    dismissed = [x for x in attention + recs if x["key"] in gone]
    attention = [x for x in attention if x["key"] not in gone]
    recs = [x for x in recs if x["key"] not in gone]

    recent = sorted(((s["attributes"].get("friendly_name", eid), _parse_ts(s["attributes"].get("last_triggered")))
                     for eid, s in automations), key=lambda x: x[1] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    activity = [{"name": n, "at": ts.astimezone().strftime("%Y-%m-%dT%H:%M:%S")} for n, ts in recent[:10] if ts]

    domains: dict[str, int] = {}
    for eid in st:
        domains[eid.split(".")[0]] = domains.get(eid.split(".")[0], 0) + 1
    versions = {
        "core": config.get("version"),
        "os": st.get("update.home_assistant_operating_system_update", {}).get("attributes", {}).get("installed_version"),
    }
    remote = st.get("binary_sensor.remote_ui")
    result = {
        "checked_at": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "banner": {"connected": True, "state": config.get("state"), "location": config.get("location_name"),
                   "versions": versions, "remote_ui": None if not remote else remote["state"] == "on"},
        "tiles": {
            "entities": total_entities, "unavailable": unavailable["count"],
            "automations_on": sum(1 for _, s in automations if s["state"] == "on"),
            "automations_off": sum(1 for _, s in automations if s["state"] != "on"),
            "updates": len(updates),
            "low_batteries": sum(1 for b in batteries if b["low"] and f"ha:battery:{b['entity_id']}" not in gone),
            "attention": len(attention),
        },
        "attention": attention,
        "recommendations": recs,
        "dismissed": dismissed,
        "unavailable": unavailable,
        "updates": updates,
        "batteries": batteries,
        "activity": activity,
        "trend": [],
    }
    result["trend"] = record_run(result, db_path, now)
    return result


# ---------------------------------------------------------------------------
# dismissing reviewed items (shared hub_dismissed table: same service as the Today page)
# ---------------------------------------------------------------------------


def _dismissed(db_path) -> set[str]:
    try:
        return {k for k in myhub.dismissed_rows(db_path=db_path) if k.startswith("ha:")}
    except sqlite3.Error:
        return set()


def dismiss(key: str, text: str = "", db_path: Path | str | None = None) -> dict:
    if not key.startswith("ha:"):
        raise HAError("not a Home Assistant item", 2)
    myhub.dismiss_key(key, text, db_path=db_path)
    return {"dismissed": key}


def restore(key: str | None = None, db_path: Path | str | None = None) -> dict:
    """Un-dismiss one item, or every Home Assistant item when key is None."""
    if key is not None and not key.startswith("ha:"):
        raise HAError("not a Home Assistant item", 2)
    keys = [key] if key else sorted(_dismissed(db_path))
    for k in keys:
        myhub.restore(k, db_path=db_path)
    return {"restored": len(keys)}


# ---------------------------------------------------------------------------
# history of health runs
# ---------------------------------------------------------------------------


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def record_run(result: dict, db_path: Path | str | None, now: datetime) -> list[dict]:
    """Store a compact summary at most every few minutes; return the recent trend (oldest first)."""
    try:
        conn = connect(db_path)
    except (sqlite3.Error, OSError):
        return []
    try:
        last = conn.execute("SELECT checked_at FROM ha_health_runs ORDER BY checked_at DESC LIMIT 1").fetchone()
        gap_ok = True
        if last:
            prev = datetime.fromisoformat(last["checked_at"])
            gap_ok = (now.replace(tzinfo=None) - prev).total_seconds() >= HEALTH_RUN_MIN_GAP_MINUTES * 60
        if gap_ok:
            summary = {k: result["tiles"][k] for k in ("entities", "unavailable", "updates", "low_batteries", "attention")}
            conn.execute("INSERT INTO ha_health_runs (id, checked_at, summary_json) VALUES (?,?,?)",
                         (str(uuid.uuid4()), result["checked_at"], json.dumps(summary)))
            conn.commit()
        rows = conn.execute("SELECT checked_at, summary_json FROM ha_health_runs ORDER BY checked_at DESC LIMIT 14").fetchall()
        return [{"checked_at": r["checked_at"], **json.loads(r["summary_json"])} for r in reversed(rows)]
    except sqlite3.Error:
        return []
    finally:
        conn.close()
