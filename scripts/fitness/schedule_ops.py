"""Read/write operations against the COROS Training Hub calendar itself
(t.coros.com/admin/views/schedule), as opposed to the Workout Library
(library_sync.py).

Phase 2 scope: `list-week` only (read-only schedule inspection). Writing
workouts to specific days (drag-to-schedule, custom-workout creation) is
Phase 3.

Usage:
    python scripts/fitness/schedule_ops.py list-week [--headless]
        Prints every card currently visible in the 3-week calendar grid
        (the visible window is whatever week the site last had open —
        "Today" by default on a fresh session), grouped by absolute date
        (YYYYMMDD, read directly from the day-cell header's DOM id, not
        computed from week/day offsets).

    python scripts/fitness/schedule_ops.py add-existing-workout
        --date 20260824 --coros-workout-id 444159851300569094 [--headless]
        Schedules an existing library workout onto a day via the real
        Coros API (GET /training/program/detail + POST
        /training/schedule/update) — NOT drag-and-drop. See "Why API, not
        drag" below. Prints {"date", "coros_workout_id", "name",
        "scheduled": bool, "cards_on_day": [...]}.

    python scripts/fitness/schedule_ops.py remove-workout
        --date 20260824 --scheduled-id 479720746862690610 [--headless]
        Deletes a single scheduled workout card (the id from
        `list-week`'s `scheduled_id`, NOT the library's
        `coros_workout_id`) via its hover trash icon + confirm dialog.

Requires a logged-in session at state/coros_session.json (see
login_test.py) and COROS_USERNAME/COROS_PASSWORD in the repo-root .env as
a login fallback.

Known gap (2026-08-20): name extraction is tuned for *scheduled/planned*
workout cards (`data-type='program'`, e.g. from a workout the fitness
agent scheduled), which is what the fitness plan actually needs to read
back. Already-completed activity cards (past GPS-logged runs pulled in
from the watch) render with a different leaf order and currently show the
activity duration instead of a name — harmless for this use case (we
never write those), but don't rely on `name` for a completed-activity
card.

--- Why API, not drag (2026-08-21) ---
Dragging a library workout onto a calendar day (both via Playwright mouse
simulation AND via real OS-level input through the browser extension) was
flaky — roughly 2 successes in 8 attempts across both mechanisms, with no
error on failure (it just silently doesn't drop). A hover-then-pause
before the drag gesture improved but didn't reliably fix this. Network
capture during a successful drag found the real call the site's own JS
makes: `POST /training/schedule/update`, body
`{entities: [{happenDay, idInPlan, sortNoInSchedule}], programs: [<full
program object>], versionObjects: [{id, status: 1}], pbVersion: 2}`.

`programs[0]` is exactly the response of
`GET /training/program/detail?id=<coros_workout_id>` (minus the
`exerciseBarChart` key, which the site's own client strips before
posting) with `idInPlan` added — i.e. not a guessed/reconstructed shape,
it's real server data round-tripped with one field changed
(`entities[0].happenDay`, the target date).

`idInPlan` (mirrored into `versionObjects[0].id`) was confirmed live to
be a simple incrementing integer, NOT tied to the workout or the date.
The API calls above need an `accessToken` header beyond plain cookies
(`credentials: 'include'` alone 401s with "Access token is invalid") —
its value is the `CPL-coros-token` cookie, read and forwarded explicitly.
`GET /training/schedule/query?startDate=&endDate=&supportRestExercise=1`
(same header required; dates as YYYYMMDD, `supportRestExercise` as the
string `"1"` not `"true"`) returns a `maxIdInPlan` field directly in its
response — the live current maximum, not a guess — so the next safe
`idInPlan` is always `int(maxIdInPlan) + 1`, queried fresh before every
write rather than tracked locally. This is self-correcting even if the
user also schedules things via the Coros app between fitness-agent runs.
"""

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from coros_web import LOGIN_URL, SCHEDULE_SELECTORS, SCHEDULE_URL, SELECTORS, TEAMAPI_BASE
from coros_session import ENV_FILE, SESSION_FILE, load_env

ERROR_SCREENSHOT = Path(__file__).resolve().parent / "last_error.png"

# Sport-type codes seen on scheduled-card `data-st` attributes so far
# (discovered live 2026-08-20; incomplete — extend as new types are seen).
SPORT_CODE_MAP = {
    "402": "strength",
}

EXTRACT_JS = r"""
() => {
  const days = [...document.querySelectorAll('.calender-day')];
  return days
    .map((d) => {
      const header = d.querySelector('.calender-day-header');
      if (!header || !/^\d{8}$/.test(header.id)) return null;
      const cards = [...d.querySelectorAll('.calender-day-card-item')].map((c) => {
        const leaves = [...c.querySelectorAll('*')]
          .filter((e) => e.children.length === 0)
          .map((e) => e.textContent.trim())
          .filter(Boolean);
        return {
          scheduled_id: c.dataset.id || null,
          sport_code: c.dataset.st || null,
          // leaves[0] is a score/load badge, leaves[1] is the workout name.
          name: leaves[1] || null,
        };
      });
      return { date: header.id, cards };
    })
    .filter(Boolean);
}
"""


def is_logged_in(page) -> bool:
    try:
        page.goto(SCHEDULE_URL, wait_until="domcontentloaded", timeout=30000)
    except PlaywrightTimeoutError:
        return False
    page.wait_for_timeout(2000)
    return "/login" not in page.url


def do_login(page, username: str, password: str) -> None:
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_selector(SELECTORS["login_email"], timeout=45000)
    page.fill(SELECTORS["login_email"], username)
    page.fill(SELECTORS["login_password"], password)
    page.locator(SELECTORS["login_agree_checkbox"]).nth(1).click()
    page.click(SELECTORS["login_submit"])
    page.wait_for_timeout(3000)


def ensure_logged_in(page) -> None:
    if is_logged_in(page):
        return
    env = load_env(ENV_FILE)
    username = env.get("COROS_USERNAME")
    password = env.get("COROS_PASSWORD")
    if not username or not password:
        raise RuntimeError(
            f"Not logged in and COROS_USERNAME/COROS_PASSWORD not set in {ENV_FILE}"
        )
    do_login(page, username, password)
    if not is_logged_in(page):
        raise RuntimeError("Login did not appear to succeed.")
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    page.context.storage_state(path=str(SESSION_FILE))


def cmd_list_week(page) -> list[dict]:
    page.wait_for_selector(SCHEDULE_SELECTORS["calendar_day_cell"], timeout=15000)
    page.wait_for_timeout(500)
    days = page.evaluate(EXTRACT_JS)
    for day in days:
        for card in day["cards"]:
            card["sport_type"] = SPORT_CODE_MAP.get(card["sport_code"])
    return days


def _get_access_token(page) -> str:
    for cookie in page.context.cookies():
        if cookie["name"] == "CPL-coros-token":
            return cookie["value"]
    raise RuntimeError("CPL-coros-token cookie not found — not logged in?")


def _get_max_id_in_plan(page, headers: dict, around_date: str) -> int:
    """Query a wide date range (the year containing `around_date`) and
    read `maxIdInPlan` from the response — the live current maximum, not
    a guess. See "Why API, not drag" above."""
    year = around_date[:4]
    resp = page.request.get(
        f"{TEAMAPI_BASE}/training/schedule/query",
        params={
            "startDate": f"{year}0101",
            "endDate": f"{year}1231",
            "supportRestExercise": "1",
        },
        headers=headers,
    )
    result = resp.json()
    if result.get("result") != "0000":
        raise RuntimeError(f"schedule/query failed: {result}")
    return int(result["data"]["maxIdInPlan"])


def cmd_add_existing_workout(page, date: str, coros_workout_id: str) -> dict:
    """Schedule an existing library workout via the real Coros API —
    see this module's "Why API, not drag" docstring section."""
    headers = {"accessToken": _get_access_token(page)}

    detail_resp = page.request.get(
        f"{TEAMAPI_BASE}/training/program/detail",
        params={"id": coros_workout_id},
        headers=headers,
    )
    detail = detail_resp.json()
    if detail.get("result") != "0000":
        raise RuntimeError(f"program/detail failed for id {coros_workout_id}: {detail}")
    program = detail["data"]
    program.pop("exerciseBarChart", None)
    name = program.get("name")

    existing_days = cmd_list_week(page)
    existing_day = next((d for d in existing_days if d["date"] == date), None)
    sort_no = len(existing_day["cards"]) if existing_day else 0

    id_in_plan = _get_max_id_in_plan(page, headers, date) + 1
    program["idInPlan"] = id_in_plan
    body = {
        "entities": [
            {"happenDay": date, "idInPlan": id_in_plan, "sortNoInSchedule": sort_no}
        ],
        "programs": [program],
        "versionObjects": [{"id": id_in_plan, "status": 1}],
        "pbVersion": 2,
    }
    update_resp = page.request.post(
        f"{TEAMAPI_BASE}/training/schedule/update",
        headers={**headers, "Content-Type": "application/json"},
        data=json.dumps(body),
    )
    result = update_resp.json()
    if result.get("result") != "0000":
        raise RuntimeError(f"schedule/update failed: {result}")

    # page.request bypasses the page's own JS/DOM state, so the calendar
    # won't show the new entry until we reload.
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector(SCHEDULE_SELECTORS["calendar_day_cell"], timeout=15000)
    page.wait_for_timeout(500)
    days = page.evaluate(EXTRACT_JS)
    day = next((d for d in days if d["date"] == date), {"date": date, "cards": []})
    # `schedule/update` returning "0000" is the authoritative success
    # signal; the DOM-based `name` check that follows is a secondary
    # sanity check only, since newly-created cards render without the
    # leading score-badge leaf (see this module's "Known gap" note above)
    # and can't always be matched by name reliably.
    scheduled = len(day["cards"]) > sort_no
    return {
        "date": date,
        "coros_workout_id": coros_workout_id,
        "name": name,
        "scheduled": scheduled,
        "cards_on_day": day["cards"],
    }


def cmd_remove_workout(page, date: str, scheduled_id: str) -> dict:
    day_body = page.locator(f"#calender-day-body-{date}")
    if day_body.count() == 0:
        raise RuntimeError(f"No calendar day found for date {date}.")
    card = day_body.locator(f"[data-id='{scheduled_id}']")
    if card.count() == 0:
        raise RuntimeError(f"No scheduled card with id {scheduled_id} on {date}.")
    card.first.hover()
    page.wait_for_timeout(300)
    card.first.locator(SCHEDULE_SELECTORS["scheduled_card_delete_icon"]).first.click()
    page.wait_for_selector(SCHEDULE_SELECTORS["delete_confirm_ok"], timeout=5000)
    page.locator(SCHEDULE_SELECTORS["delete_confirm_ok"]).click()
    page.wait_for_timeout(800)

    days = page.evaluate(EXTRACT_JS)
    day = next((d for d in days if d["date"] == date), {"date": date, "cards": []})
    still_present = any(c["scheduled_id"] == scheduled_id for c in day["cards"])
    return {"date": date, "scheduled_id": scheduled_id, "removed": not still_present}


def run(args) -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=args.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            storage_state=str(SESSION_FILE) if SESSION_FILE.exists() else None,
            viewport={"width": 1600, "height": 1000},
        )
        page = context.new_page()
        try:
            ensure_logged_in(page)
            if args.command == "list-week":
                result = cmd_list_week(page)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                print(f"[schedule_ops] {len(result)} days found.", file=sys.stderr)
            elif args.command == "add-existing-workout":
                result = cmd_add_existing_workout(page, args.date, args.coros_workout_id)
                print(json.dumps(result, ensure_ascii=False, indent=2))
            elif args.command == "remove-workout":
                result = cmd_remove_workout(page, args.date, args.scheduled_id)
                print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        except Exception as exc:  # noqa: BLE001 — diagnostic script
            try:
                page.screenshot(path=str(ERROR_SCREENSHOT))
                print(f"[error] Screenshot saved to {ERROR_SCREENSHOT}", file=sys.stderr)
            except Exception:  # noqa: BLE001
                pass
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1
        finally:
            browser.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "command", choices=["list-week", "add-existing-workout", "remove-workout"]
    )
    ap.add_argument("--date", help="YYYYMMDD")
    ap.add_argument(
        "--coros-workout-id",
        help="library workout id (fitness_workout_library.coros_workout_id; add-existing-workout)",
    )
    ap.add_argument("--scheduled-id", help="scheduled card id from list-week (remove-workout)")
    ap.add_argument("--headless", action="store_true", help="hide the browser")
    args = ap.parse_args()
    if args.command == "add-existing-workout" and not (args.date and args.coros_workout_id):
        ap.error("add-existing-workout requires --date and --coros-workout-id")
    if args.command == "remove-workout" and not (args.date and args.scheduled_id):
        ap.error("remove-workout requires --date and --scheduled-id")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
