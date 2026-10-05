"""Cross-platform locations for config, secrets and the Claude CLI.

Everything that used to read USERPROFILE/APPDATA goes through here so the
repo has one place that knows where things live. Import with
`from paths import ...` (scripts run by path have their own directory first
on sys.path).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR_NAME = "scherbring-assistant"


def claude_home() -> Path:
    return Path.home() / ".claude"


def telegram_env_path() -> Path:
    """The Telegram channel plugin's own .env (holds TELEGRAM_BOT_TOKEN)."""
    return claude_home() / "channels" / "telegram" / ".env"


def secrets_dir() -> Path:
    """Directory for secrets outside the repo. SCHERBRING_CONFIG_DIR overrides."""
    override = os.environ.get("SCHERBRING_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / CONFIG_DIR_NAME


def find_claude() -> str | None:
    """Path to the claude CLI, or None. Checks PATH, then ~/.local/bin."""
    found = shutil.which("claude")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / "claude"
    return str(candidate) if candidate.exists() else None


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def get_env(key: str, extra_files: tuple[Path, ...] = ()) -> str | None:
    """Look up a setting. First hit wins:
    process env, secrets_dir()/.env, repo .env, then any extra_files.
    Empty values count as unset.
    """
    value = os.environ.get(key)
    if value:
        return value
    for path in (secrets_dir() / ".env", REPO_ROOT / ".env", *extra_files):
        value = _parse_env_file(path).get(key)
        if value:
            return value
    return None
