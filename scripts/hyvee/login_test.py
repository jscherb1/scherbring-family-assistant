"""Phase 1 diagnostic: log into Hy-Vee (Aisles Online) and add one item to cart.

Standalone test that proves login + add-to-cart automation works before any
real feature (preferences, Todoist, subagent) is built. It NEVER checks out or
places an order.

Selectors below were discovered against the live site (2026-07). Hy-Vee's login
is Auth0 (identity.hy-vee.com); the storefront is a SPA on www.hy-vee.com with
stable data-testid attributes.

Usage:
    pip install -r scripts/hyvee/requirements.txt
    playwright install chromium
    python scripts/hyvee/login_test.py                # headed, watch it work
    python scripts/hyvee/login_test.py --headless     # no visible window
    python scripts/hyvee/login_test.py --item bananas # different test item

Requires HYVEE_USERNAME / HYVEE_PASSWORD in the repo-root .env file.
"""

import argparse
import sys
from pathlib import Path

from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

# --- Paths ---
REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"
SESSION_FILE = REPO_ROOT / "state" / "hyvee_session.json"
ERROR_SCREENSHOT = Path(__file__).resolve().parent / "last_error.png"

# --- URLs ---
HOME_URL = "https://www.hy-vee.com/"
LOGIN_URL = "https://www.hy-vee.com/main/login"

# --- Selectors (discovered from the live site) ---
SEL_COOKIE_ACCEPT = "#onetrust-accept-btn-handler"
SEL_USERNAME = "#username"
SEL_PASSWORD = "#password"
SEL_MFA = (
    "input[autocomplete='one-time-code'], input[name*='code' i], "
    "input[id*='code' i]"
)
SEL_LOGIN_LINK = "[data-testid='global-navigation-login']"
SEL_CART_ICON = "[data-testid='global-navigation-cart-icon-button']"
SEL_CART_BUBBLE = "[data-testid='global-navigation-cart-bubble']"
SEL_SEARCH_INPUT = "[data-testid='global-navigation-search-input']"
SEL_ADD_TO_CART = "[data-testid='add-to-cart-button']"


def load_env(path: Path) -> dict:
    """Minimal .env parser (stdlib only) — KEY=VALUE lines, ignores blanks/#."""
    values = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def dismiss_cookie_banner(page) -> None:
    """Accept the OneTrust cookie banner if it's covering the page."""
    try:
        btn = page.locator(SEL_COOKIE_ACCEPT).first
        if btn.is_visible(timeout=3000):
            btn.click()
            page.wait_for_timeout(1000)
    except PlaywrightTimeoutError:
        pass


def is_logged_in(page) -> bool:
    """Logged in when the cart icon is present and the Log In link is gone."""
    try:
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
    except PlaywrightTimeoutError:
        return False
    page.wait_for_timeout(2000)
    dismiss_cookie_banner(page)
    try:
        if page.locator(SEL_LOGIN_LINK).first.is_visible(timeout=2000):
            return False
    except PlaywrightTimeoutError:
        pass
    try:
        return page.locator(SEL_CART_ICON).first.is_visible(timeout=3000)
    except PlaywrightTimeoutError:
        return False


def do_login(page, username: str, password: str) -> None:
    print("[login] Navigating to sign-in (Auth0)...")
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2000)
    dismiss_cookie_banner(page)

    page.fill(SEL_USERNAME, username)
    page.fill(SEL_PASSWORD, password)
    # Submit via Enter — a cookie overlay can intercept a button click.
    page.press(SEL_PASSWORD, "Enter")
    page.wait_for_timeout(6000)

    # MFA: this account did not prompt in testing, but handle it if it appears.
    try:
        field = page.locator(SEL_MFA).first
        if field.is_visible(timeout=2000):
            code = input(
                "\n[mfa] A verification code field appeared. "
                "Enter the code you received: "
            ).strip()
            field.fill(code)
            field.press("Enter")
            page.wait_for_timeout(5000)
    except PlaywrightTimeoutError:
        pass


def cart_quantity(page) -> int:
    """Read the item-count badge on the cart icon (0 if the bubble is absent)."""
    try:
        text = page.locator(SEL_CART_BUBBLE).first.inner_text(timeout=3000)
    except PlaywrightTimeoutError:
        return 0
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else 0


def add_test_item_to_cart(page, item: str) -> bool:
    print(f"[cart] Cart quantity before: ", end="")
    before = cart_quantity(page)
    print(before)

    print(f"[cart] Searching for '{item}'...")
    box = page.locator(SEL_SEARCH_INPUT).first
    box.click()
    box.fill(item)
    box.press("Enter")
    page.wait_for_url("**/aisles-online/search**", timeout=20000)
    page.wait_for_timeout(4000)
    dismiss_cookie_banner(page)

    print("[cart] Adding first result to cart...")
    add_button = page.locator(SEL_ADD_TO_CART).first
    add_button.scroll_into_view_if_needed(timeout=10000)
    add_button.click(timeout=15000)
    page.wait_for_timeout(4000)

    after = cart_quantity(page)
    print(f"[cart] Cart quantity after: {after}")
    return after > before


def run(args) -> int:
    env = load_env(ENV_FILE)
    username = env.get("HYVEE_USERNAME")
    password = env.get("HYVEE_PASSWORD")
    if not username or not password:
        print(
            f"ERROR: HYVEE_USERNAME and HYVEE_PASSWORD must be set in {ENV_FILE}",
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
                        "Login did not appear to succeed (no cart icon / "
                        "Log In link still present)."
                    )
                context.storage_state(path=str(SESSION_FILE))
                print(f"[login] Session saved to {SESSION_FILE}")

            added = add_test_item_to_cart(page, args.item)
            result = "PASS" if added else "FAIL"
            print(f"\n{result}: add-to-cart test for '{args.item}'")

            if args.keep_open:
                input("\nPress Enter to close the browser...")
            return 0 if added else 1

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
    ap.add_argument("--item", default="milk", help="test item to search/add")
    ap.add_argument("--headless", action="store_true", help="hide the browser")
    ap.add_argument(
        "--keep-open",
        action="store_true",
        help="pause for Enter before closing (headed manual runs)",
    )
    return run(ap.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
