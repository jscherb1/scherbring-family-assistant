"""Scrape the COROS Training Hub Workout Library into JSON on stdout.

Phase 2 of the fitness agent: the Workout Library is the source of truth
for reusable workouts (see docs/superpowers/specs/2026-08-20-fitness-agent-
coros-login-design.md). This script logs in (reusing the saved session from
login_test.py when valid), opens the library panel on the schedule page,
and extracts every workout card's data-id/data-name plus its visible stats
and sport-type icon slug via a single page.evaluate — DOM scraping, not an
API replay (no stable JSON API for this panel was found during discovery).

Output feeds scripts/fitness_store.py directly:
    python scripts/fitness/library_sync.py | python scripts/fitness_store.py workout sync

Usage:
    python scripts/fitness/library_sync.py [--headless]

Requires COROS_USERNAME / COROS_PASSWORD in the repo-root .env as a login
fallback if state/coros_session.json is missing or stale.
"""

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from coros_web import SCHEDULE_SELECTORS, SCHEDULE_URL, SELECTORS
from coros_session import ENV_FILE, SESSION_FILE, load_env

ERROR_SCREENSHOT = Path(__file__).resolve().parent / "last_error.png"

EXTRACT_JS = r"""
() => {
  const cards = [...document.querySelectorAll('.training-group-item.card-dragable')];
  return cards.map((c) => {
    const statEls = [...c.querySelectorAll('.arco-spin *')]
      .filter((e) => e.children.length === 0)
      .map((e) => e.textContent.trim())
      .filter(Boolean);
    // statEls[0] is always the workout name itself (duplicates
    // c.dataset.name); the rest render as alternating label/value pairs,
    // e.g. ["Sets", "15 set(s)", "Target distance", "4.8 mi", "Target
    // time", "36 min", "Estimated load", "165 TL"].
    const rest = statEls.slice(1);
    const pairs = {};
    for (let i = 1; i < rest.length; i += 2) {
      pairs[rest[i - 1]] = rest[i];
    }
    const sportIcon = c.querySelector("[class*='iconfont-sport']");
    const m = sportIcon ? sportIcon.className.match(/icon-([a-z0-9]+)/) : null;
    return {
      coros_workout_id: c.dataset.id,
      name: c.dataset.name,
      workout_type: m ? m[1] : null,
      sets_desc: pairs['Sets'] || null,
      target_distance: pairs['Target distance'] || null,
      target_time: pairs['Target time'] || null,
      estimated_load: pairs['Estimated load'] || null,
    };
  });
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
    from coros_web import LOGIN_URL

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


def fetch_library(page) -> list[dict]:
    """Open the Workout Library panel and collect every card.

    The panel's card list is virtualized (confirmed live 2026-08-20): only
    ~20 cards are ever mounted in the DOM at once, older ones unmount as
    you scroll past them, and it ignores direct `scrollTop` assignment —
    it only responds to real wheel events. So this scrolls in small steps
    via `page.mouse.wheel`, re-extracting and merging (by
    coros_workout_id) after every step, and stops once several consecutive
    scrolls stop turning up anything new.
    """
    toggle = page.locator(SCHEDULE_SELECTORS["workouts_panel_toggle"])
    if page.locator(SCHEDULE_SELECTORS["workouts_panel"]).count() == 0:
        toggle.click()
    page.wait_for_selector(SCHEDULE_SELECTORS["workout_card"], timeout=15000)
    page.wait_for_timeout(500)

    panel = page.locator(SCHEDULE_SELECTORS["workouts_panel"])
    box = panel.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

    collected: dict[str, dict] = {}
    stable_rounds = 0
    for item in page.evaluate(EXTRACT_JS):
        collected[item["coros_workout_id"]] = item

    for _ in range(60):  # hard safety cap on scroll steps
        page.mouse.wheel(0, 600)
        page.wait_for_timeout(400)
        before = len(collected)
        for item in page.evaluate(EXTRACT_JS):
            collected[item["coros_workout_id"]] = item
        stable_rounds = stable_rounds + 1 if len(collected) == before else 0
        if stable_rounds >= 4:
            break

    return list(collected.values())


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
            workouts = fetch_library(page)
            print(json.dumps(workouts, ensure_ascii=False, indent=2))
            print(f"[library_sync] {len(workouts)} workouts found.", file=sys.stderr)
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
    ap.add_argument("--headless", action="store_true", help="hide the browser")
    return run(ap.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
