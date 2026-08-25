#!/usr/bin/env python3
"""Rolling short-term memory bridge for Telegram conversations across orchestrator restarts.

Problem: start_orchestrator.ps1 always launches a brand-new Claude session (by
design, to avoid unbounded context growth across restarts - see its 2026-08-12
note). That means a watchdog-triggered restart (stale tool binding, wedged
event loop, etc.) used to wipe whatever conversation was in progress, and the
watchdog had to warn the user "I won't remember this" before killing the
process.

This script maintains a small rolling JSONL log of Telegram traffic in BOTH
directions (state/telegram_conversation_log.jsonl), pruned to the last
WINDOW_MINUTES on every write plus on a standalone `prune` call, so it never
grows unbounded regardless of how many restarts happen or how long the
orchestrator runs between them. On a fresh session start, whatever's left in
that rolling window (usually nothing - most restarts land during quiet
periods) gets injected as context automatically via a SessionStart hook, and
if the last logged message was from the user with no reply logged after it,
the injected context says so explicitly so the new session picks it up.

Subcommands (all read/write state/telegram_conversation_log.jsonl):
  hook-post-reply   PostToolUse hook for mcp__plugin_telegram_telegram__reply
                     - logs an outbound message from tool_input.
  hook-post-bash     PostToolUse hook for Bash - logs an outbound message
                     ONLY when the command invoked telegram_send.py (the
                     stale-binding fallback), since that's the other path a
                     reply can leave this orchestrator.
  hook-user-prompt   UserPromptSubmit hook - logs an inbound message when the
                     submitted prompt carries a Telegram <channel> tag.
  hook-session-start SessionStart hook - prunes, then emits the remaining
                     window as additionalContext (empty if nothing recent).
  prune              Standalone prune with no stdin, for periodic invocation
                     from run_watchdog.ps1 so entries age out even during long
                     gaps with no Telegram traffic to trigger a write.

All hook subcommands read a single JSON object from stdin (the harness's
standard hook payload) and are deliberately best-effort: malformed/missing
fields are swallowed so a bug here never blocks the orchestrator loop.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = REPO_ROOT / "state" / "telegram_conversation_log.jsonl"
CONFIG_FILE = REPO_ROOT / "scripts" / "scheduler.config.json"
WINDOW_MINUTES = 60

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _load_entries() -> list[dict]:
    if not LOG_FILE.exists():
        return []
    entries = []
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _save_entries(entries: list[dict]) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + ("\n" if entries else ""),
        encoding="utf-8",
    )


def _prune(entries: list[dict]) -> list[dict]:
    cutoff = _now_utc() - timedelta(minutes=WINDOW_MINUTES)
    kept = []
    for e in entries:
        try:
            if _parse_iso(e["ts"]) >= cutoff:
                kept.append(e)
        except (KeyError, ValueError):
            continue
    return kept


def _append(direction: str, chat_id: str | None, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return
    entries = _prune(_load_entries())
    entries.append(
        {
            "ts": _utc_iso(_now_utc()),
            "direction": direction,
            "chat_id": chat_id,
            "text": text,
        }
    )
    _save_entries(entries)


def _read_stdin_json() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def _default_chat_id() -> str | None:
    try:
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        chat_id = config.get("alert_chat_id")
        return str(chat_id) if chat_id else None
    except Exception:  # noqa: BLE001
        return None


def cmd_hook_post_reply() -> int:
    payload = _read_stdin_json()
    tool_input = payload.get("tool_input") or {}
    chat_id = tool_input.get("chat_id")
    text = tool_input.get("text")
    if text:
        _append("out", chat_id, text)
    return 0


_TELEGRAM_SEND_RE = re.compile(
    r"telegram_send\.py\s+(?P<q>[\"'])(?P<text>(?:(?!(?P=q)).)*)(?P=q)",
    re.DOTALL,
)
_CHAT_ID_FLAG_RE = re.compile(r"--chat-id[= ]+(\S+)")


def cmd_hook_post_bash() -> int:
    payload = _read_stdin_json()
    command = (payload.get("tool_input") or {}).get("command") or ""
    if "telegram_send.py" not in command:
        return 0
    match = _TELEGRAM_SEND_RE.search(command)
    if not match:
        return 0
    text = match.group("text")
    chat_match = _CHAT_ID_FLAG_RE.search(command)
    chat_id = chat_match.group(1) if chat_match else _default_chat_id()
    _append("out", chat_id, text)
    return 0


_CHANNEL_TAG_RE = re.compile(
    r'<channel\s+source="plugin:telegram:telegram"[^>]*\bchat_id="(?P<chat_id>[^"]+)"[^>]*>'
    r"(?P<body>.*?)</channel>",
    re.DOTALL,
)


def cmd_hook_user_prompt() -> int:
    payload = _read_stdin_json()
    prompt = None
    for key in ("prompt", "user_prompt", "message", "text"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            prompt = val
            break
    if not prompt:
        return 0
    match = _CHANNEL_TAG_RE.search(prompt)
    if not match:
        return 0
    _append("in", match.group("chat_id"), match.group("body"))
    return 0


def cmd_hook_session_start() -> int:
    # Consume stdin even though we don't need its contents, so the harness
    # never sees a broken pipe.
    _read_stdin_json()
    entries = _prune(_load_entries())
    _save_entries(entries)

    if not entries:
        return 0

    now = _now_utc()
    lines = ["Recent Telegram conversation carried over from before this restart:"]
    for e in entries:
        try:
            age_min = round((now - _parse_iso(e["ts"])).total_seconds() / 60)
        except (KeyError, ValueError):
            age_min = "?"
        who = "User" if e.get("direction") == "in" else "Assistant"
        lines.append(f"[{age_min}m ago] {who}: {e.get('text', '')}")

    if entries[-1].get("direction") == "in":
        lines.append(
            "\nThe message above from the user has NOT been replied to yet - "
            "a restart interrupted before a response went out. Check it and "
            "respond now via the Telegram reply tool, using chat_id "
            f"{entries[-1].get('chat_id')}."
        )

    context = "\n".join(lines)
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": context,
                }
            }
        )
    )
    return 0


def cmd_prune() -> int:
    entries = _prune(_load_entries())
    _save_entries(entries)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: telegram_context_bridge.py <hook-post-reply|hook-post-bash|hook-user-prompt|hook-session-start|prune>", file=sys.stderr)
        return 2

    command = argv[0]
    dispatch = {
        "hook-post-reply": cmd_hook_post_reply,
        "hook-post-bash": cmd_hook_post_bash,
        "hook-user-prompt": cmd_hook_user_prompt,
        "hook-session-start": cmd_hook_session_start,
        "prune": cmd_prune,
    }
    func = dispatch.get(command)
    if not func:
        print(f"unknown command: {command}", file=sys.stderr)
        return 2

    try:
        return func()
    except Exception as exc:  # noqa: BLE001
        # Best-effort by design: a bug here must never block the orchestrator
        # or a Telegram send/receive.
        print(f"telegram_context_bridge: {command} failed: {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
