"""Source: COROS sleep, steps and stress per day, from COROS's official MCP server.

Sleep is dated by wake-up day, so a day's sleep is the night before it. Bedtime is hours after noon
(23:07 = 11.1, 00:30 = 12.5) so late nights sort correctly; wake time is the hour of day.
Needs the one-time sign-in (`python scripts/fitness/coros_mcp.py auth-url`); if the token can't be
renewed this source reports an error and the rest of the refresh carries on.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "fitness"))

SOURCE = {"name": "coros_health", "kind": "auto",
          "description": "COROS sleep (hours, bed/wake time, score, deep/REM), steps, exercise minutes and stress per day"}


def to_metrics(sleep_text: str, health_text: str, stress_text: str) -> dict:
    import coros_mcp
    merged: dict[str, dict[str, float]] = {}
    for parsed in (coros_mcp.parse_daily_health(health_text), coros_mcp.parse_stress_level(stress_text),
                   coros_mcp.parse_sleep_overview(sleep_text)):  # later parsers win on overlap
        for day, row in parsed.items():
            merged.setdefault(day, {}).update(row)
    out: dict[str, dict[str, float]] = {}
    for day, row in merged.items():
        for name, value in row.items():
            out.setdefault(f"coros:{name}", {})[day] = value
    return out


def collect(conn, start, end, today):
    import coros_mcp
    days = (today - start).days + 1
    sleep, health, stress = coros_mcp.call_tools([
        ("querySleepOverview", {"startDate": start.strftime("%Y%m%d"), "endDate": end.strftime("%Y%m%d")}),
        ("queryDailyHealthData", {"days": days}),
        ("queryStressLevel", {"days": days})])
    out = to_metrics(sleep, health, stress)
    for name in ("steps", "exercise_min", "stress_avg"):  # today is still in progress, so these are partial
        out.get(f"coros:{name}", {}).pop(today.isoformat(), None)
    return out
