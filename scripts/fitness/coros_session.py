"""Shared path constants and env/session helpers for the COROS automation scripts.

Mirrors scripts/hyvee/hyvee_session.py so login_test.py and future
scripts/fitness/*.py all reuse the same env parser and session-file
conventions instead of re-implementing them.
"""

import sys
from pathlib import Path

# --- Paths ---
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from paths import load_env as _load_env, secrets_dir  # noqa: E402
ENV_FILE = secrets_dir() / ".env"
SESSION_FILE = REPO_ROOT / "state" / "coros_session.json"


def load_env(path: Path = ENV_FILE) -> dict:
    """Merged settings (process env, ~/.config/scherbring-assistant/.env, repo .env).

    ``path`` is kept for caller compatibility; lookup order lives in paths.load_env.
    """
    return _load_env()
