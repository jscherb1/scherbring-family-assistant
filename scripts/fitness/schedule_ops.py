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
"""

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from coros_web import LOGIN_URL, SCHEDULE_SELECTORS, SCHEDULE_URL, SELECTORS
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
    ap.add_argument("command", choices=["list-week"])
    ap.add_argument("--headless", action="store_true", help="hide the browser")
    return run(ap.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
