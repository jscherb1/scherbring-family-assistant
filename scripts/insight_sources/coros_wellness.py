"""Source: COROS recovery and load per day (overnight HRV, resting heart rate, fatigue, training load).

Reads Training Hub's /analyse/dayDetail/query with the saved COROS web session (same login the
workout sync uses; if the session expired it logs in again, which signs COROS out elsewhere).
Metric days with no value (watch not worn) are skipped rather than stored as zero.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "fitness"))

SOURCE = {"name": "coros_wellness", "kind": "auto",
          "description": "COROS overnight HRV, resting heart rate, fatigue and training load per day"}

# COROS field -> (metric name, keep zero values?)
FIELDS = {"avgSleepHrv": ("hrv_ms", False), "rhr": ("resting_hr", False),
          "tiredRateNew": ("fatigue", True), "trainingLoad": ("training_load", True),
          "trainingLoadRatio": ("load_ratio", False), "t7d": ("load_7d", True)}


def fetch_day_details(start, end) -> list[dict]:
    from coros_session import SESSION_FILE
    from coros_web import TEAMAPI_BASE
    from playwright.sync_api import sync_playwright
    from schedule_ops import _get_access_token, ensure_logged_in

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(storage_state=str(SESSION_FILE) if SESSION_FILE.exists() else None)
        page = context.new_page()
        try:
            ensure_logged_in(page)
            resp = page.request.get(f"{TEAMAPI_BASE}/analyse/dayDetail/query",
                                    headers={"accessToken": _get_access_token(page)},
                                    params={"startDay": start.strftime("%Y%m%d"), "endDay": end.strftime("%Y%m%d")})
            body = resp.json()
            if body.get("result") != "0000":
                raise RuntimeError(f"dayDetail/query failed: {body.get('message') or body}")
            return body["data"].get("dayList") or []
        finally:
            browser.close()


def to_metrics(day_list: list[dict]) -> dict:
    out: dict[str, dict[str, float]] = {}
    for d in day_list:
        raw = str(d.get("happenDay", ""))
        if len(raw) != 8:
            continue
        day = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
        for field, (name, keep_zero) in FIELDS.items():
            v = d.get(field)
            if v is None or (v == 0 and not keep_zero):
                continue
            out.setdefault(f"coros:{name}", {})[day] = float(v)
        hrv, base = d.get("avgSleepHrv"), d.get("sleepHrvBase")
        if hrv and base:
            out.setdefault("coros:hrv_vs_baseline", {})[day] = float(hrv - base)
    return out


def collect(conn, start, end, today):
    return to_metrics(fetch_day_details(start, end))
