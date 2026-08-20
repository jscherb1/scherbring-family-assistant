"""Phase 1 diagnostic: log into COROS Training Hub and persist a session.

Standalone proof that login automation works before any real feature
(library sync, schedule writes, subagent) is built. Mirrors
scripts/hyvee/login_test.py's reuse-session-first structure.

Selectors below were discovered against the live site (2026-08). Login
lives directly on t.coros.com/login (Arco Design form, no separate
identity subdomain) and redirects back to the page it came from on
success.

Usage:
    pip install -r scripts/fitness/requirements.txt
    playwright install chromium
    python scripts/fitness/login_test.py                # headed, watch it work
    python scripts/fitness/login_test.py --headless     # no visible window

Requires COROS_USERNAME / COROS_PASSWORD in the repo-root .env file.
"""

import argparse
import sys
from pathlib import Path

from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from coros_web import LOGIN_URL, SCHEDULE_URL, SELECTORS
from coros_session import ENV_FILE, SESSION_FILE, load_env

# --- Paths ---
ERROR_SCREENSHOT = Path(__file__).resolve().parent / "last_error.png"

# --- Selectors (see coros_web.py — the single source of truth) ---
SEL_EMAIL = SELECTORS["login_email"]
SEL_PASSWORD = SELECTORS["login_password"]
SEL_AGREE_CHECKBOX = SELECTORS["login_agree_checkbox"]
SEL_SUBMIT = SELECTORS["login_submit"]


def is_logged_in(page) -> bool:
    """Logged in when navigating to the schedule doesn't bounce to /login."""
    try:
        page.goto(SCHEDULE_URL, wait_until="domcontentloaded", timeout=30000)
    except PlaywrightTimeoutError:
        return False
    page.wait_for_timeout(2000)
    return "/login" not in page.url


def detect_captcha(page) -> bool:
    """Best-effort CAPTCHA detection — the account tested against has never
    shown one, but Coros (like Hy-Vee) may introduce one for new devices or
    after failed attempts, so surface it explicitly instead of failing with
    a confusing timeout."""
    try:
        return page.locator(
            "iframe[src*='captcha' i], [class*='captcha' i]"
        ).first.is_visible(timeout=2000)
    except PlaywrightTimeoutError:
        return False


def do_login(page, username: str, password: str) -> None:
    print("[login] Navigating to login page...")
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_selector(SEL_EMAIL, timeout=45000)

    page.fill(SEL_EMAIL, username)
    page.fill(SEL_PASSWORD, password)
    # Index 1 is the required "I have read and agree to the COROS Privacy
    # Policy" box (index 0 is "Remember me") — see coros_web.py.
    page.locator(SEL_AGREE_CHECKBOX).nth(1).click()

    if detect_captcha(page):
        raise RuntimeError(
            "CAPTCHA detected on login page. Re-run with --headed and solve "
            "it manually; this script does not attempt to bypass it."
        )

    page.click(SEL_SUBMIT)
    page.wait_for_timeout(3000)

    if detect_captcha(page):
        raise RuntimeError(
            "CAPTCHA detected after submitting login. Re-run with --headed "
            "and solve it manually."
        )


def run(args) -> int:
    env = load_env(ENV_FILE)
    username = env.get("COROS_USERNAME")
    password = env.get("COROS_PASSWORD")
    if not username or not password:
        print(
            f"ERROR: COROS_USERNAME and COROS_PASSWORD must be set in {ENV_FILE}",
            file=sys.stderr,
        )
        return 2

    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=args.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            storage_state=str(SESSION_FILE) if SESSION_FILE.exists() else None,
            viewport={"width": 1366, "height": 900},
        )
        page = context.new_page()

        try:
            if is_logged_in(page):
                print("[login] Reusing saved session — already signed in.")
            else:
                print("[login] Not signed in — logging in fresh.")
                do_login(page, username, password)
                if not is_logged_in(page):
                    raise RuntimeError(
                        "Login did not appear to succeed (still redirected "
                        "to /login after submitting)."
                    )
                context.storage_state(path=str(SESSION_FILE))
                print(f"[login] Session saved to {SESSION_FILE}")

            print("PASS: logged in and schedule page reachable.")

            if args.keep_open:
                input("\nPress Enter to close the browser...")
            return 0

        except Exception as exc:  # noqa: BLE001 — diagnostic script
            try:
                page.screenshot(path=str(ERROR_SCREENSHOT))
                print(f"[error] Screenshot saved to {ERROR_SCREENSHOT}")
            except Exception:  # noqa: BLE001
                pass
            print(f"FAIL: {exc}", file=sys.stderr)
            if args.keep_open:
                input("\nError occurred. Press Enter to close the browser...")
            return 1
        finally:
            browser.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--headless", action="store_true", help="hide the browser")
    ap.add_argument(
        "--keep-open",
        action="store_true",
        help="pause for Enter before closing (headed manual runs)",
    )
    return run(ap.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
