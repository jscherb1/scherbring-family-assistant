"""Shared path constants and env/session helpers for the Hy-Vee automation scripts.

Extracted from login_test.py so login_test.py, diagnose.py, and the future
cart_ops.py (Task 8) all reuse the exact same env parser and cookie-banner
dismissal logic instead of re-implementing it.
"""

import sys
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from hyvee_web import LOGIN_URL, SELECTORS

# --- Paths ---
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from paths import load_env as _load_env, secrets_dir  # noqa: E402
ENV_FILE = secrets_dir() / ".env"
SESSION_FILE = REPO_ROOT / "state" / "hyvee_session.json"


def load_env(path: Path = ENV_FILE) -> dict:
    """Merged settings (process env, ~/.config/scherbring-assistant/.env, repo .env).

    ``path`` is kept for caller compatibility; lookup order lives in paths.load_env.
    """
    return _load_env()


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
