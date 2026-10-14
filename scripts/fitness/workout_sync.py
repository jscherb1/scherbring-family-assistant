"""Log the "Workouts" habit from COROS recorded activities (COROS is the source of truth).

Any recorded activity (run, strength, Peloton, bike, sauna, ...) on a day counts, and several
activities on one day are one workout. Planned-but-not-done workouts and the Workouts calendar are
never consulted. Idempotent: a day that already has a habit log is skipped, so it is safe to re-run
and to backfill.

Usage:
    python scripts/fitness/workout_sync.py [--days 3] [--habit Workouts] [--dry-run] [--headed]
        Looks at the last N days (today included) and logs one completion per day that has a COROS
        activity and no log yet. Prints JSON {"checked_days", "activity_days", "logged", "skipped",
        "dry_run"}. Exit 0 ok, 1 COROS login/API failure (message on stderr).
"""

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from coros_session import REPO_ROOT, SESSION_FILE  # noqa: E402
from coros_web import TEAMAPI_BASE  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import habits  # noqa: E402

NOTE = "auto: COROS activity"


def activity_days(activities: list[dict]) -> dict[str, list[str]]:
    """Group COROS activity names by local ISO date (the API's `date` is YYYYMMDD, local)."""
    days: dict[str, list[str]] = {}
    for a in activities:
        raw = str(a.get("date", ""))
        if len(raw) != 8 or not raw.isdigit():
            continue
        iso = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
        days.setdefault(iso, []).append(a.get("name") or a.get("customName") or "activity")
    return days


def sync(days: dict[str, list[str]], habit_ref: str = "Workouts", *, db_path=None,
         dry_run: bool = False, today: date | None = None) -> dict:
    """Log one completion for each activity day that has none yet (pure over the habits service)."""
    habit = habits.resolve_habit(habit_ref, db_path)
    have = {l["log_date"] for l in habits.list_logs(habit["id"], limit=None, db_path=db_path)}
    logged, skipped = [], []
    for day in sorted(days):
        if day in have:
            skipped.append(day)
            continue
        if not dry_run:
            habits.log_completion(habit["id"], log_date=day, note=f"{NOTE}: {', '.join(days[day])}"[:200],
                                  db_path=db_path, today=today)
        logged.append(day)
    return {"activity_days": sorted(days), "logged": logged, "skipped": skipped, "dry_run": dry_run}


def fetch_activities(start: date, end: date, headed: bool = False) -> list[dict]:
    from playwright.sync_api import sync_playwright
    from schedule_ops import _get_access_token, ensure_logged_in

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed,
                                    args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(storage_state=str(SESSION_FILE) if SESSION_FILE.exists() else None)
        page = context.new_page()
        try:
            ensure_logged_in(page)
            headers = {"accessToken": _get_access_token(page)}
            out, number = [], 1
            while True:
                resp = page.request.get(f"{TEAMAPI_BASE}/activity/query", headers=headers, params={
                    "size": "50", "pageNumber": str(number), "modeList": "",
                    "startDay": start.strftime("%Y%m%d"), "endDay": end.strftime("%Y%m%d")})
                body = resp.json()
                if body.get("result") != "0000":
                    raise RuntimeError(f"activity/query failed: {body.get('message') or body}")
                rows = body["data"].get("dataList") or []
                out.extend(rows)
                if len(rows) < 50 or number >= 10:
                    return out
                number += 1
        finally:
            browser.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=3, help="look back this many days, today included")
    ap.add_argument("--habit", default="Workouts")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    if args.days < 1:
        ap.error("--days must be 1 or more")
    today = date.today()
    start = today - timedelta(days=args.days - 1)
    try:
        activities = fetch_activities(start, today, args.headed)
        result = sync(activity_days(activities), args.habit, dry_run=args.dry_run)
    except (RuntimeError, habits.HabitError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return getattr(exc, "code", 1) or 1
    result["checked_days"] = args.days
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
