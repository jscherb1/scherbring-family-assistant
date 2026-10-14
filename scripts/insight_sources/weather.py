"""Source: daily weather from the free Open-Meteo archive/forecast APIs (no key needed).

Location comes from weather_config (latitude/longitude, set for the weather-reminders agent), falling
back to Rochester, MN. Past days come from the archive API; the last few days (not archived yet) from
the forecast API's past_days window.
"""
import json
import urllib.parse
import urllib.request
from datetime import timedelta

SOURCE = {"name": "weather", "kind": "auto",
          "description": "Daily temperature, precipitation, snow, wind and daylight (Open-Meteo, free)"}

DEFAULT_LAT, DEFAULT_LON = "44.0234", "-92.4630"
DAILY = {"temperature_2m_max": "high_f", "temperature_2m_min": "low_f", "precipitation_sum": "precip_in",
         "snowfall_sum": "snow_in", "wind_speed_10m_max": "wind_mph", "sunshine_duration": "sunshine_hours",
         "cloud_cover_mean": "cloud_pct"}


def _config(conn, key, default):
    r = conn.execute("SELECT value FROM weather_config WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else default


def _get(url, params):
    with urllib.request.urlopen(f"{url}?{urllib.parse.urlencode(params)}", timeout=30) as resp:
        return json.load(resp)


def _merge(out, payload):
    daily = payload.get("daily") or {}
    for api_key, name in DAILY.items():
        for day, v in zip(daily.get("time", []), daily.get(api_key, [])):
            if v is None:
                continue
            if api_key == "sunshine_duration":
                v = v / 3600.0
            out.setdefault(f"weather:{name}", {})[day] = round(float(v), 2)
    if "weather:precip_in" in out:
        out["weather:rainy_day"] = {d: 1.0 if v >= 0.05 else 0.0 for d, v in out["weather:precip_in"].items()}


def collect(conn, start, end, today):
    lat, lon = _config(conn, "latitude", DEFAULT_LAT), _config(conn, "longitude", DEFAULT_LON)
    base = {"latitude": lat, "longitude": lon, "timezone": "America/Chicago",
            "temperature_unit": "fahrenheit", "precipitation_unit": "inch", "wind_speed_unit": "mph",
            "daily": ",".join(DAILY)}
    out = {}
    archive_end = min(end, today - timedelta(days=5))
    if archive_end >= start:
        _merge(out, _get("https://archive-api.open-meteo.com/v1/archive",
                         {**base, "start_date": start.isoformat(), "end_date": archive_end.isoformat()}))
    recent_start = max(start, today - timedelta(days=6))
    if recent_start <= end:
        payload = _get("https://api.open-meteo.com/v1/forecast",
                       {**base, "past_days": 7, "forecast_days": 1})
        daily = payload.get("daily") or {}
        keep = [i for i, d in enumerate(daily.get("time", [])) if recent_start.isoformat() <= d <= end.isoformat()]
        payload["daily"] = {k: [v[i] for i in keep] for k, v in daily.items()}
        _merge(out, payload)
    return out
