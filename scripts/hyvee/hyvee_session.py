"""Shared path constants and env/session helpers for the Hy-Vee automation scripts.

Extracted from login_test.py so login_test.py, diagnose.py, and the future
cart_ops.py (Task 8) all reuse the exact same env parser and cookie-banner
dismissal logic instead of re-implementing it.
"""

from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from hyvee_web import LOGIN_URL, SELECTORS

# --- Paths ---
REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"
SESSION_FILE = REPO_ROOT / "state" / "hyvee_session.json"


def load_env(path: Path = ENV_FILE) -> dict:
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
        btn = page.locator(SELECTORS["cookie_accept"]).first
        if btn.is_visible(timeout=3000):
            btn.click()
            page.wait_for_timeout(1000)
    except PlaywrightTimeoutError:
        pass


def goto_login(page, timeout: int = 45000) -> None:
    """Navigate to the sign-in page, tolerating the Auth0 redirect race.

    ``www.hy-vee.com/main/login`` immediately client-redirects to Auth0's
    hosted login form on ``identity.hy-vee.com``. That second navigation
    supersedes the one Playwright's ``goto()`` is tracking, so ``goto()``
    reports ``net::ERR_ABORTED`` even though the redirect chain completes
    fine a moment later (confirmed live 2026-08-02: the error screenshot
    from a failed run showed the Auth0 login form, fully rendered, at the
    moment of the "failure"). Swallow that specific error and instead
    confirm success by waiting for the actual login form to appear.
    """
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=timeout)
    except PlaywrightError as exc:
        if "ERR_ABORTED" not in str(exc):
            raise
    page.wait_for_selector(SELECTORS["username"], timeout=timeout)
