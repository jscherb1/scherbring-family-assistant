"""Body: latest COROS recovery, sleep, activity, heart and wellness-check readings."""
from ._util import count_days, latest, series

PROVIDER = {"name": "body", "title": "Body"}

TILES = [  # (key, metric, label, unit)
    ("steps", "coros:steps", "Steps", ""), ("stress", "coros:stress_avg", "Stress", ""),
    ("resting_hr", "coros:resting_hr", "Resting HR", "bpm"), ("hrv", "coros:hrv_ms", "HRV", "ms"),
    ("load", "coros:training_load", "Load", ""), ("exercise", "coros:exercise_min", "Exercise", "min"),
]
WELLNESS = [("hr", "coros:wellness_hr", "Heart rate", "bpm"), ("hrv", "coros:wellness_hrv", "HRV", "ms"),
            ("stress", "coros:wellness_stress", "Stress", ""), ("resp", "coros:wellness_resp", "Breathing", "/min"),
            ("spo2", "coros:wellness_spo2", "Blood oxygen", "%")]


def _clock(v):
    """Hours (0-24) to 'h:mm AM/PM'."""
    v = v % 24
    h, m = int(v), int(round((v - int(v)) * 60))
    if m == 60:
        h, m = h + 1, 0
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h % 24 < 12 else 'PM'}"


def collect(conn, today):
    def val(metric, within=3):
        r = latest(conn, metric, today, within)
        return {"day": r[0], "value": r[1]} if r else None

    sleep = {k: val(m, 4) for k, m in [("hours", "coros:sleep_hours"), ("score", "coros:sleep_score"),
                                        ("deep", "coros:deep_pct"), ("light", "coros:light_pct"),
                                        ("rem", "coros:rem_pct"), ("awake_min", "coros:awake_min"),
                                        ("wakeups", "coros:wakeups"), ("bed", "coros:bedtime_after_noon"),
                                        ("wake", "coros:wake_hour")]}
    if sleep["bed"]:
        sleep["bed_label"] = _clock(12 + sleep["bed"]["value"])
    if sleep["wake"]:
        sleep["wake_label"] = _clock(sleep["wake"]["value"])
    rec = val("coros:recovery_pct", 2)
    tiles = []
    for key, metric, label, unit in TILES:
        v = val(metric)
        if v:
            tiles.append({"key": key, "label": label, "unit": unit, "value": v["value"], "day": v["day"],
                          "spark": list(series(conn, metric, today, 14).values())})
    wellness = []
    for key, metric, label, unit in WELLNESS:
        v = val(metric, 7)
        wellness.append({"key": key, "label": label, "unit": unit, "reading": v, "days_on_file": count_days(conn, metric)})
    attention = []
    newest = latest(conn, "coros:sleep_hours", today, 14)
    if newest is None:
        attention.append({"key": f"body:nodata:{today}", "href": "/rating/insights", "level": "warn",
                          "text": "No COROS sleep data in the last 2 weeks (sign-in may need a refresh)"})
    return {"card": {"recovery": rec, "recovery_hours_left": val("coros:recovery_hours_left", 2), "sleep": sleep,
                     "tiles": tiles, "wellness": wellness, "has_data": bool(newest)},
            "attention": attention}
