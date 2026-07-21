"""Shared path constants and env/session helpers for the Hy-Vee automation scripts.

Extracted from login_test.py so login_test.py, diagnose.py, and the future
cart_ops.py (Task 8) all reuse the exact same env parser and cookie-banner
dismissal logic instead of re-implementing it.
"""

from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from hyvee_web import SELECTORS

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
