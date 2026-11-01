"""Guard rails: the gmail agent and project allow list must stay read-only."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = ROOT / ".claude" / "settings.json"
AGENT = ROOT / ".claude" / "agents" / "gmail.md"

READ_TOOLS = {
    "mcp__claude_ai_Gmail__search_threads",
    "mcp__claude_ai_Gmail__get_thread",
    "mcp__claude_ai_Gmail__get_message",
    "mcp__claude_ai_Gmail__list_labels",
}
WRITE_WORDS = ("send", "draft", "forward", "reply", "trash", "delete", "label", "spam")


def _frontmatter():
    text = AGENT.read_text()
    return dict(
        line.split(":", 1) for line in text.split("---")[1].strip().splitlines() if ":" in line
    )


def test_gmail_agent_tools_are_exactly_the_read_tools():
    fm = _frontmatter()
    tools = {t.strip() for t in fm["tools"].split(",")}
    assert tools == READ_TOOLS
    assert "Bash" not in tools
    assert fm["model"].strip() == "sonnet"


def test_no_allow_rule_grants_gmail_write_tools():
    allow = json.loads(SETTINGS.read_text())["permissions"]["allow"]
    gmail = [r for r in allow if r.startswith("mcp__claude_ai_Gmail__")]
    assert READ_TOOLS <= set(gmail)
    for rule in gmail:
        name = rule.split("__", 2)[2]
        if name == "list_labels":
            continue
        assert not re.search("|".join(WRITE_WORDS), name), rule
    assert "mcp__claude_ai_Gmail" not in allow and "mcp__claude_ai_Gmail__*" not in allow
