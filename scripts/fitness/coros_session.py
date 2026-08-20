"""Shared path constants and env/session helpers for the COROS automation scripts.

Mirrors scripts/hyvee/hyvee_session.py so login_test.py and future
scripts/fitness/*.py all reuse the same env parser and session-file
conventions instead of re-implementing them.
"""

from pathlib import Path

# --- Paths ---
REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"
SESSION_FILE = REPO_ROOT / "state" / "coros_session.json"


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
