"""Rivian (R2) status and trends: the single shared service.

Zero-dependency (Python stdlib only). Read-only: it never calls a Home Assistant service. Everything comes from
the `rivian` HACS integration's entities in Home Assistant, read through scripts/ha.py (same credentials, cache and
error handling as the Family Board). Both front ends are thin wrappers over this module:
  - scripts/rivian_store.py      the CLI the `ha-overview` subagent runs ("how charged is the car?")
  - dashboard/routers/rivian.py  the Home > Home Assistant > Rivian page and its /api/ha/rivian twin

status() is the live snapshot (energy, charging, vehicle, closures, tires, climate, health exceptions).
trends(days) is daily history from HA long-term statistics (SOC, range, miles driven, estimated efficiency).
Failures raise RivianError with a CLI-style code: 1 = not found / unreachable, 2 = bad input.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import ha

PREFIX = "r2"  # the HA device is named "R2"; every entity id is <domain>.r2_<key>
STALE_HOURS = 6
MAX_ETA_MIN = 72 * 60  # longer estimates (e.g. a 120 V trickle charge) read as "3+ days", not a date
TREND_DAYS = (7, 30)
IMAGE_VIEWS = ("three_quarter", "side", "side_charging", "side_rear", "front", "rear", "overhead", "in_use")
TIRES = (("front_left", "FL", "Front left"), ("front_right", "FR", "Front right"),
         ("rear_left", "RL", "Rear left"), ("rear_right", "RR", "Rear right"))
DOORS = (("front_left", "Front left door"), ("front_right", "Front right door"),
         ("rear_left", "Rear left door"), ("rear_right", "Rear right door"))

CHARGER_STATES = {
    "charging_ready": "Plugged in, ready", "charging_connecting": "Connecting", "charging_active": "Charging",
    "charging_complete": "Charge complete", "charging_scheduled": "Scheduled to charge",
    "smart_charging_active": "Smart charging", "smart_charging_paused": "Smart charging paused",
    "waiting_on_charger": "Waiting on charger", "charging_stopped_by_user": "Stopped by you",
    "charging_stopped_by_station": "Stopped by the station", "charging_vehicle_stopped": "Stopped by the car",
    "charging_sd_compensation": "Charging",
}
ACTIVE_CHARGER_STATES = {"charging_active", "smart_charging_active", "charging_sd_compensation"}
DERATE = {
    "warm_adapter": "The charging adapter is warm, so charging is slowed.",
    "dc_warm_plug": "The fast-charge plug is warm, so charging is slowed.",
    "ac_warm_plug": "The charge plug is warm, so charging is slowed.",
    "evse_derating": "The charger itself is limiting the power.",
    "nearing_toc": "Slowing down as the battery nears full.",
    "near_toc_lfp_batt_calibrating": "Topping off slowly to calibrate the battery.",
    "hvac_prioritized": "Some power is going to cabin climate.",
    "battery_heating": "Some power is warming the battery.",
    "battery_cooling": "Some power is cooling the battery.",
    "cell_thermal_lim_cold": "The battery is cold, so charging is slowed.",
    "cell_thermal_lim_cold_no_current": "The battery is too cold to charge yet.",
    "cell_thermal_lim_hot": "The battery is warm, so charging is slowed.",
    "cell_thermal_lim_hot_no_current": "The battery is too warm to charge right now.",
    "pack_hardware_thermal_lim": "Battery hardware temperature is limiting the power.",
    "high_soc_sigma": "Balancing cells near full.",
    "hv_battery_fault": "A high-voltage battery fault is limiting charging.",
    "dcac_export": "The car is exporting power.",
}
POWER_STATES = {"sleep": "Asleep", "standby": "Standby", "ready": "Awake", "go": "Driving",
                "vehicle_reset": "Restarting", "ota_update": "Installing update", "shutdown": "Off"}
DRIVE_MODES = {"everyday": "All-Purpose", "sport": "Sport", "distance": "Conserve", "winter": "Snow",
               "towing": "Towing", "sport_launch": "Launch", "off_road_auto": "All-Terrain",
               "off_road_snow_ice": "Snow", "off_road_sand": "Sand", "off_road_rocks": "Rock Crawl",
               "off_road_mud": "Mud", "off_road_sport_auto": "Rally", "off_road_sport_drift": "Drift"}
TIRE_STATUS = {"ok": "OK", "warning_soft": "Low", "warning_hard": "Very low", "warning_puncture": "Puncture"}
RANGE_ZONE = {"normal": "good", "low": "mid", "red": "bad", "critically_low": "bad"}
OTA_LABELS = {"idle": "Up to date", "ready_to_download": "Update ready to download", "downloading": "Downloading update",
              "ready_to_install": "Update ready to install", "scheduled_to_install": "Update scheduled",
              "awaiting_install": "Update waiting to install", "installing": "Installing update",
              "install_success": "Update installed", "download_failed": "Update download failed",
              "install_failed": "Update install failed", "fault": "Update fault"}
MISSING = ("unknown", "unavailable", "", None)


class RivianError(Exception):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.message = message
        self.code = code


def eid(domain: str, key: str) -> str:
    return f"{domain}.{PREFIX}_{key}"


class _Car:
    """Typed accessors over the HA state map for this one vehicle."""

    def __init__(self, all_states: dict):
        self.s = all_states

    def raw(self, domain: str, key: str):
        st = self.s.get(eid(domain, key))
        return None if st is None or st["state"] in MISSING else st["state"]

    def num(self, key: str, domain: str = "sensor") -> float | None:
        return ha._num(self.raw(domain, key))

    def on(self, key: str) -> bool | None:
        v = self.raw("binary_sensor", key)
        return None if v is None else v == "on"

    def attrs(self, domain: str, key: str) -> dict:
        st = self.s.get(eid(domain, key))
        return st["attributes"] if st else {}

    def changed(self, domain: str, key: str) -> datetime | None:
        st = self.s.get(eid(domain, key))
        return ha._parse_ts(st.get("last_changed")) if st else None


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(ha.local_tz())


def _r(v, nd: int = 0):
    if v is None:
        return None
    return int(round(v)) if nd == 0 else round(v, nd)


def _label(value, table: dict) -> str | None:
    if value is None:
        return None
    return table.get(value) or value.replace("_", " ").capitalize()


def _duration(minutes) -> str | None:
    if minutes is None or minutes < 0:
        return None
    h, m = divmod(int(round(minutes)), 60)
    return f"{h} h {m} min" if h else f"{m} min"


def _clock(dt: datetime, now: datetime) -> str:
    t = dt.strftime("%-I:%M %p")
    if dt.date() == now.date():
        return f"today {t}"
    if dt.date() == now.date() + timedelta(days=1):
        return f"tomorrow {t}"
    return dt.strftime("%a ") + t


def _ago(dt: datetime | None, now: datetime) -> str | None:
    if dt is None:
        return None
    mins = max(0, int((now - dt).total_seconds() // 60))
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    if mins < 48 * 60:
        return f"{mins // 60} h ago"
    return f"{mins // 1440} days ago"


def _states() -> dict:
    try:
        st = ha.states()
    except ha.HAError as exc:
        raise RivianError(exc.message, exc.code) from None
    if eid("sensor", "battery_state_of_charge") not in st:
        raise RivianError(f"No Rivian in Home Assistant (expected {eid('sensor', 'battery_state_of_charge')}).", 1)
    return st


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def _energy(c: _Car) -> dict:
    soc, limit, rng, cap = c.num("battery_state_of_charge"), c.num("battery_state_of_charge_limit"), \
        c.num("estimated_vehicle_range"), c.num("battery_capacity")
    threshold = c.raw("sensor", "range_threshold")
    zone = RANGE_ZONE.get(threshold, "good")
    if soc is not None and soc < 20:
        zone = "bad"
    elif soc is not None and soc < 35 and zone == "good":
        zone = "mid"
    cold = c.raw("sensor", "cold_range_notification")
    return {
        "soc": _r(soc, 1), "soc_display": f"{_r(soc)}%" if soc is not None else "—",
        "limit": _r(limit), "range_mi": _r(rng), "capacity_kwh": _r(cap, 1),
        "kwh_remaining": _r(soc * cap / 100, 1) if soc is not None and cap is not None else None,
        "range_threshold": threshold, "zone": zone,
        "miles_per_pct": _r(rng / soc, 2) if rng and soc else None,
        "cold_impact": cold in ("cold_may_impact", "cold_impact"),
        "cold_label": {"cold_may_impact": "Cold may reduce range", "cold_impact": "Cold is reducing range"}.get(cold),
    }


def _charging(c: _Car, now: datetime) -> dict:
    connected = bool(c.on("charger_connection"))
    state = c.raw("sensor", "charger_state")
    active = state in ACTIVE_CHARGER_STATES or (bool(c.on("charging_status")) and connected)
    remaining = c.num("charging_time_remaining")
    start = ha._parse_ts(c.raw("sensor", "charging_start_time"))
    elapsed_s = c.num("charging_time_elapsed")
    eta = now + timedelta(minutes=remaining) if active and remaining and 0 < remaining <= MAX_ETA_MIN else None
    derate = c.raw("sensor", "charger_derate_status")
    fault = c.raw("sensor", "charging_fault")
    sched_on = c.raw("switch", "charging_schedule_enabled") == "on"
    s_start, s_end = c.raw("time", "charging_schedule_start"), c.raw("time", "charging_schedule_end")

    def hhmm(v):
        try:
            return datetime.strptime(v, "%H:%M:%S").strftime("%-I:%M %p")
        except (TypeError, ValueError):
            return None

    return {
        "connected": connected, "active": active, "state": state,
        "label": _label(state, CHARGER_STATES) if connected or active else "Not plugged in",
        "kw": _r(c.num("charging_speed"), 1), "mph": _r(c.num("charging_rate")),
        "remaining_min": _r(remaining), "remaining_display": (None if not active or remaining is None else "3+ days at this rate"
                              if remaining > MAX_ETA_MIN else _duration(remaining)),
        "eta": eta.isoformat() if eta else None, "eta_display": _clock(eta, now) if eta else None,
        "energy_kwh": _r(c.num("charging_energy_delivered"), 1), "range_added_mi": _r(c.num("charging_range_added")),
        "cost": _r(c.num("charging_cost"), 2),
        "started": start.astimezone(ha.local_tz()).isoformat() if start and connected else None,
        "elapsed_display": _duration(elapsed_s / 60) if elapsed_s and connected else None,
        "derate": derate if derate not in (None, "none") else None,
        "derate_text": DERATE.get(derate) if derate not in (None, "none") else None,
        "fault": fault if fault not in (None, "none") else None,
        "schedule": {
            "enabled": sched_on, "start": hhmm(s_start), "end": hhmm(s_end),
            "days": (c.raw("sensor", "charging_schedule_days") or "").replace("_", " ") or None,
            "amps": _r(c.num("charging_schedule_amperage", "number")),
            "display": (f"{hhmm(s_start)}–{hhmm(s_end)}, {(c.raw('sensor', 'charging_schedule_days') or 'daily').replace('_', ' ')}"
                        if sched_on and s_start and s_end else "Off"),
        },
    }


def _vehicle(c: _Car, now: datetime) -> dict:
    tracker = c.s.get(f"device_tracker.{PREFIX}") or {"state": None, "attributes": {}}
    where = tracker["state"]
    location = None if where in MISSING else "Home" if where == "home" else "Away" if where == "not_home" \
        else where.replace("_", " ").title()
    moved = ha._parse_ts(tracker.get("last_changed"))
    upd = c.s.get(f"update.{PREFIX}_software") or {"state": None, "attributes": {}}
    ota = c.raw("sensor", "software_ota_status")
    power = c.raw("sensor", "power_state")
    return {
        "power": power, "power_label": _label(power, POWER_STATES),
        "gear": (c.raw("sensor", "gear_selector") or "").replace("not_defined", "").capitalize() or None,
        "drive_mode": _label(c.raw("sensor", "drive_mode"), DRIVE_MODES),
        "speed_mph": _r(c.num("speed")), "moving": bool(c.on("in_use_state")),
        "driver": c.raw("sensor", "active_driver"),
        "odometer_mi": _r(c.num("odometer")),
        "location": location, "home": where == "home",
        "location_since": _ago(moved, now) if moved else None,
        "latitude": tracker["attributes"].get("latitude"), "longitude": tracker["attributes"].get("longitude"),
        "destination": c.raw("sensor", "destination"),
        "software": upd["attributes"].get("installed_version"),
        "software_latest": upd["attributes"].get("latest_version"),
        "update_available": upd["state"] == "on",
        "ota": ota, "ota_label": _label(ota, OTA_LABELS),
        "trailer": c.raw("sensor", "trailer_status") in ("trailer_present", "trailer_present_with_brakes"),
    }


def _closures(c: _Car) -> dict:
    def item(key, label, lock_key=None, kind="door"):
        open_ = c.on(key)
        locked = None if lock_key is None or c.on(lock_key) is None else not c.on(lock_key)
        return {"key": key, "label": label, "kind": kind, "open": open_, "locked": locked}

    doors = [item(f"door_{k}", lbl, f"door_{k}_lock") for k, lbl in DOORS]
    windows = [item(f"window_{k}", lbl.replace("door", "window"), kind="window") for k, lbl in DOORS]
    windows.append(item("window_liftgate", "Liftgate window", kind="window"))
    frunk = item("front_trunk", "Frunk", "front_trunk_lock")
    liftgate = item("liftgate", "Liftgate", "liftgate_lock")
    everything = doors + windows + [frunk, liftgate]
    opened = [i["label"] for i in everything if i["open"]]
    locked_state = c.on("locked_state")
    return {
        "locked": None if locked_state is None else not locked_state,
        "all_closed": not opened if any(i["open"] is not None for i in everything) else None,
        "open": opened, "doors": doors, "windows": windows, "frunk": frunk, "liftgate": liftgate,
        "gear_guard_alarm": bool(c.on("gear_guard_alarm")),
    }


def _tires(c: _Car) -> list[dict]:
    out = []
    for key, short, label in TIRES:
        status = c.raw("sensor", f"tire_pressure_{key}_status")
        out.append({"key": key, "short": short, "label": label, "psi": _r(c.num(f"tire_pressure_{key}")),
                    "status": status, "status_label": _label(status, TIRE_STATUS),
                    "ok": status in (None, "ok"),
                    "zone": "good" if status == "ok" else "bad" if status in ("warning_hard", "warning_puncture")
                    else "mid" if status == "warning_soft" else "none"})
    return out


def _climate(c: _Car) -> dict:
    return {"cabin_f": _r(c.num("cabin_temperature")), "preconditioning": bool(c.on("cabin_climate_preconditioning")),
            "pet_mode": bool(c.on("pet_mode")), "defrost": bool(c.on("defrost_defog"))}


def _health(c: _Car, energy: dict, charging: dict, vehicle: dict, tires: list, closures: dict,
            stale: bool, updated_ago: str | None) -> list[dict]:
    items: list[dict] = []

    def add(severity, title, detail=""):
        items.append({"severity": severity, "title": title, "detail": detail})

    for t in tires:
        if not t["ok"]:
            add("bad" if t["zone"] == "bad" else "mid", f"{t['label']} tire: {t['status_label'].lower()}",
                f"{t['psi']} psi" if t["psi"] is not None else "")
    if c.raw("sensor", "12v_battery_health") == "low":
        add("bad", "12V battery is low", "The car may have trouble waking up.")
    if c.raw("sensor", "battery_thermal_status") == "detected":
        add("bad", "Battery thermal event detected", "Check the Rivian app now.")
    if c.raw("sensor", "brake_fluid_level") == "active":
        add("bad", "Brake fluid is low")
    if c.on("wiper_fluid_level"):
        add("mid", "Washer fluid is low")
    if charging["fault"]:
        add("bad", "Charging fault", charging["fault"].replace("_", " "))
    if closures["gear_guard_alarm"]:
        add("bad", "Gear Guard alarm triggered")
    if (c.num("limited_acceleration_cold") or 0) > 0 or (c.num("limited_regenerative_braking_cold") or 0) > 0:
        add("info", "Cold battery is limiting acceleration or regen braking")
    if energy["range_threshold"] in ("low", "red", "critically_low"):
        add("bad" if energy["zone"] == "bad" else "mid", "Range is low", f"{energy['range_mi']} mi left")
    if vehicle["ota"] in ("download_failed", "install_failed", "fault"):
        add("mid", vehicle["ota_label"])
    elif vehicle["update_available"] or vehicle["ota"] in ("ready_to_install", "awaiting_install", "scheduled_to_install"):
        add("info", "Software update available", vehicle["software_latest"] or vehicle["ota_label"] or "")
    if stale:
        add("mid", "Car hasn't reported in a while", f"Last update {updated_ago}" if updated_ago else "")
    return items


def status(now: datetime | None = None) -> dict:
    now = _now(now)
    c = _Car(_states())
    times = [ha._parse_ts(st.get("last_updated") or st.get("last_changed"))
             for k, st in c.s.items() if k.split(".", 1)[-1].startswith(PREFIX + "_") and st["state"] not in MISSING]
    times = [t for t in times if t]
    updated = max(times) if times else None
    stale = bool(c.on("vehicle_updates")) or (updated is not None and now - updated > timedelta(hours=STALE_HOURS))
    energy, charging, vehicle = _energy(c), _charging(c, now), _vehicle(c, now)
    closures, tires = _closures(c), _tires(c)
    updated_ago = _ago(updated, now)
    health = _health(c, energy, charging, vehicle, tires, closures, stale, updated_ago)
    image = "three_quarter"
    return {
        "name": "R2", "checked_at": now.isoformat(timespec="seconds"),
        "updated_at": updated.astimezone(ha.local_tz()).isoformat(timespec="seconds") if updated else None,
        "updated_ago": updated_ago, "stale": stale,
        "energy": energy, "charging": charging, "vehicle": vehicle, "closures": closures, "tires": tires,
        "climate": _climate(c), "health": health,
        "health_ok": not any(h["severity"] in ("bad", "mid") for h in health),
        "image": image,
        "images": [v for v in IMAGE_VIEWS if eid("image", f"{v}_dark") in c.s],
    }


def brief(now: datetime | None = None) -> dict:
    """The one-line answer to "how's the car?" (also the dashboard Summary tile)."""
    s = status(now)
    e, ch, v = s["energy"], s["charging"], s["vehicle"]
    parts = [f"{e['soc_display']}", f"{e['range_mi']} mi" if e["range_mi"] is not None else None]
    if ch["active"]:
        parts.append(f"charging{' at ' + str(ch['kw']) + ' kW' if ch['kw'] else ''}"
                     + (f", to {e['limit']}% {ch['eta_display']}" if ch["eta_display"] else ""))
    elif ch["connected"]:
        parts.append(ch["label"].lower())
    if v["location"]:
        parts.append(v["location"].lower() if v["location"] in ("Home", "Away") else v["location"])
    return {"headline": " · ".join(p for p in parts if p), "soc": e["soc"], "range_mi": e["range_mi"],
            "charging": ch["active"], "connected": ch["connected"], "locked": s["closures"]["locked"],
            "open": s["closures"]["open"], "health": s["health"], "updated_ago": s["updated_ago"], "stale": s["stale"]}


# ---------------------------------------------------------------------------
# trends (HA long-term statistics)
# ---------------------------------------------------------------------------


def _stat_day(row: dict, tz) -> date | None:
    start = row.get("start")
    try:
        if isinstance(start, (int, float)):
            dt = datetime.fromtimestamp(start / 1000, timezone.utc)
        else:
            dt = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
    except (TypeError, ValueError, OSError):
        return None
    return dt.astimezone(tz).date()


def _stats(days: int, now: datetime) -> tuple[dict, dict]:
    start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    daily_ids = [eid("sensor", k) for k in ("battery_state_of_charge", "estimated_vehicle_range", "odometer",
                                             "cabin_temperature")] + \
                [eid("sensor", f"tire_pressure_{k}") for k, _, _ in TIRES]
    base = {"type": "recorder/statistics_during_period", "start_time": start.astimezone(timezone.utc).isoformat(),
            "end_time": now.astimezone(timezone.utc).isoformat()}
    try:
        daily, hourly = ha._ws_cached(f"rivian:{days}", [
            {**base, "statistic_ids": daily_ids, "period": "day", "types": ["mean", "min", "max", "change"]},
            {**base, "statistic_ids": [eid("sensor", "battery_state_of_charge")], "period": "hour",
             "types": ["mean"]},
        ])
    except ha.HAError as exc:
        raise RivianError(exc.message, exc.code) from None
    return daily or {}, hourly or {}


def _by_day(rows: list, field: str, tz, nd: int = 1) -> dict[str, float]:
    out = {}
    for row in rows or []:
        d, v = _stat_day(row, tz), row.get(field)
        if d is not None and v is not None:
            out[d.isoformat()] = round(v, nd) if nd else int(round(v))
    return out


def _soc_used_by_day(hourly_rows: list, capacity_kwh: float | None, tz) -> dict[str, float]:
    """kWh drawn from the pack per local day: the sum of hour-to-hour SOC drops times capacity. Charging hours
    (rises) are ignored, and idle drain counts as use, which is why efficiency is labelled an estimate."""
    if not capacity_kwh:
        return {}
    used: dict[str, float] = {}
    prev = None
    for row in sorted(hourly_rows or [], key=lambda r: str(r.get("start"))):
        v, d = row.get("mean"), _stat_day(row, tz)
        if v is None or d is None:
            continue
        if prev is not None and v < prev:
            used[d.isoformat()] = used.get(d.isoformat(), 0.0) + (prev - v) / 100 * capacity_kwh
        prev = v
    return {k: round(v, 2) for k, v in used.items()}


def efficiency(miles: float, kwh: float) -> float | None:
    """mi/kWh, only when there's enough driving and energy for the number to mean something."""
    if miles is None or kwh is None or miles < 3 or kwh < 1:
        return None
    return round(miles / kwh, 2)


def trends(days: int = 30, now: datetime | None = None) -> dict:
    if days not in TREND_DAYS:
        raise RivianError(f"days must be one of {', '.join(map(str, TREND_DAYS))}", 2)
    now = _now(now)
    tz = ha.local_tz()
    c = _Car(_states())
    daily, hourly = _stats(days, now)
    dates = [(now.date() - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    S = lambda key: daily.get(eid("sensor", key)) or []  # noqa: E731

    soc_min, soc_mean, soc_max = (_by_day(S("battery_state_of_charge"), f, tz) for f in ("min", "mean", "max"))
    miles = {d: v for d, v in _by_day(S("odometer"), "change", tz).items() if v >= 0}
    used = _soc_used_by_day(hourly.get(eid("sensor", "battery_state_of_charge")), c.num("battery_capacity"), tz)
    eff = {d: e for d in dates if (e := efficiency(miles.get(d), used.get(d))) is not None}

    last7 = dates[-7:]
    m7 = sum(miles.get(d, 0) for d in last7)
    k7 = sum(used.get(d, 0) for d in last7)
    tires = {k: _by_day(S(f"tire_pressure_{k}"), "mean", tz) for k, _, _ in TIRES}
    have = sorted(set(soc_mean) | set(miles))
    return {
        "days": days, "dates": dates,
        "soc": {"min": soc_min, "mean": soc_mean, "max": soc_max},
        "range_mi": _by_day(S("estimated_vehicle_range"), "mean", tz, 0),
        "miles": miles, "kwh_used": used, "efficiency": eff,
        "cabin_f": _by_day(S("cabin_temperature"), "mean", tz),
        "tires": tires,
        "totals": {
            "miles": round(sum(miles.get(d, 0) for d in dates), 1),
            "miles_7d": round(m7, 1), "kwh_7d": round(k7, 1),
            "efficiency_7d": efficiency(m7, k7),
            "driving_days": sum(1 for d in dates if miles.get(d, 0) >= 0.5),
            "avg_miles_per_driving_day": round(m7 / max(1, sum(1 for d in last7 if miles.get(d, 0) >= 0.5)), 1)
            if m7 else 0,
        },
        "data_days": len(have),
        "first_day": have[0] if have else None,
        "limit": c.num("battery_state_of_charge_limit"),
    }


def image(view: str) -> tuple[bytes, str]:
    """A rendered image of the car (dark variant) from the integration's image entities."""
    if view not in IMAGE_VIEWS:
        raise RivianError("unknown image view", 2)
    entity = eid("image", f"{view}_dark")
    key = "rivian-img:" + view
    hit = ha._cache.get(key)
    if hit and ha.time.monotonic() - hit[0] < 3600:
        return hit[1]
    try:
        value = ha._open(f"/api/image_proxy/{entity}", raw=True)
    except ha.HAError as exc:
        raise RivianError(exc.message, exc.code) from None
    ha._cache[key] = (ha.time.monotonic(), value)
    return value
