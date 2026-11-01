"""Source: COROS sleep, steps and stress per day, from COROS's official MCP server.

Sleep is dated by wake-up day, so a day's sleep is the night before it. Bedtime is hours after noon
(23:07 = 11.1, 00:30 = 12.5) so late nights sort correctly; wake time is the hour of day.
Needs the one-time sign-in (`python scripts/fitness/coros_mcp.py auth-url`); if the token can't be
renewed this source reports an error and the rest of the refresh carries on.
"""
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "fitness"))

SOURCE = {"name": "coros_health", "kind": "auto",
          "description": "COROS sleep (hours, bed/wake time, score, deep/REM), overnight HRV with its normal range, "
                         "steps, exercise minutes, stress, wellness-check readings (sparse), recovery and live "
                         "training load (acute/chronic/ratio) per day"}

WELLNESS_LOOKBACK = 60  # the server returns one check per query, so each day is its own call


def to_metrics(sleep_text: str, health_text: str, stress_text: str, wellness_text: str = "",
               recovery_text: str = "", today=None, hrv_text: str = "", load_text: str = "") -> dict:
    import coros_mcp
    merged: dict[str, dict[str, float]] = {}
    for parsed in (coros_mcp.parse_daily_health(health_text), coros_mcp.parse_stress_level(stress_text),
                   coros_mcp.parse_sleep_overview(sleep_text), coros_mcp.parse_wellness_check(wellness_text)):  # later parsers win on overlap
        for day, row in parsed.items():
            merged.setdefault(day, {}).update(row)
    out: dict[str, dict[str, float]] = {}
    for day, row in merged.items():
        for name, value in row.items():
            out.setdefault(f"coros:{name}", {})[day] = value
    # Overnight HRV is the official assessment, identical to the Training Hub web series on every overlapping day
    # (see docs/TODO.md), so it owns coros:hrv_ms and the derived baseline gap and normal band.
    for day, r in coros_mcp.parse_sleep_hrv(hrv_text).items():
        out.setdefault("coros:hrv_ms", {})[day] = r["hrv_ms"]
        if "hrv_baseline" in r:
            out.setdefault("coros:hrv_vs_baseline", {})[day] = r["hrv_ms"] - r["hrv_baseline"]
        if "hrv_base_low" in r:
            out.setdefault("coros:hrv_base_low", {})[day] = r["hrv_base_low"]
            out.setdefault("coros:hrv_base_high", {})[day] = r["hrv_base_high"]
    # Daily training load from the MCP, kept under its own names: it is a different series from the weekly web
    # one (coros_wellness) and must not overwrite it.
    for day, r in coros_mcp.parse_training_load(load_text).items():
        for name, value in r.items():
            out.setdefault(f"coros:mcp_{name}", {})[day] = value
    if today:  # recovery is only a live snapshot, so it is stamped on the day it was read and builds up daily
        for name, value in coros_mcp.parse_recovery(recovery_text).items():
            out.setdefault(f"coros:{name}", {})[today.isoformat()] = value
    return out


def collect(conn, start, end, today):
    import coros_mcp
    days = (today - start).days + 1
    sleep, health, stress = coros_mcp.call_tools([
        ("querySleepOverview", {"startDate": start.strftime("%Y%m%d"), "endDate": end.strftime("%Y%m%d")}),
        ("queryDailyHealthData", {"days": days}),
        ("queryStressLevel", {"days": days})])
    wellness_days = [today - timedelta(days=i) for i in range(min(days, WELLNESS_LOOKBACK))]
    extra = coros_mcp.call_tools([("queryHealthCheckTimeSeries", {"startDate": d.strftime("%Y%m%d")})
                                  for d in wellness_days] + [("queryRecoveryStatus", {})])
    hrv, load = coros_mcp.call_tools([
        ("querySleepHrv", {"startDate": start.strftime("%Y%m%d"), "endDate": end.strftime("%Y%m%d")}),
        ("queryTrainingLoadAssessment", {"days": days})])
    out = to_metrics(sleep, health, stress, "\n".join(extra[:-1]), extra[-1], today, hrv, load)
    for name in ("steps", "exercise_min", "stress_avg"):  # today is still in progress, so these are partial
        out.get(f"coros:{name}", {}).pop(today.isoformat(), None)
    return out
