"""Guard rails: the project settings must keep denying agent access to credentials."""
import json
from pathlib import Path

SETTINGS = Path(__file__).resolve().parent.parent / ".claude" / "settings.json"

MUST_DENY = [
    "~/.config/scherbring-assistant",
    "~/.ssh",
    "~/.monarch-mcp-server",
    "~/.claude/.credentials.json",
    "./.env",
    "./state/hyvee_session.json",
    "./state/coros_session.json",
]


def _deny():
    return json.loads(SETTINGS.read_text())["permissions"]["deny"]


def test_read_and_edit_denied_for_every_secret():
    deny = _deny()
    for path in MUST_DENY:
        for tool in ("Read", "Edit"):
            assert any(r.startswith(f"{tool}({path}") for r in deny), f"{tool} not denied for {path}"


def test_no_allow_rule_conflicts_with_a_deny():
    s = json.loads(SETTINGS.read_text())["permissions"]
    for rule in s["allow"]:
        assert ".env" not in rule and "credentials" not in rule, rule
