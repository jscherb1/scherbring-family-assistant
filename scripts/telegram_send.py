#!/usr/bin/env python3
"""Direct-Bot-API fallback for sending a Telegram message.

Use this ONLY when the mcp__plugin_telegram_telegram__reply tool errors or
isn't available (e.g. "Tool ... not found in render-time tools") — a known
failure mode where an SSE stream reconnect silently drops the tool binding
without otherwise breaking the session. See memory: telegram_sse_reconnect_stale_tool.

Being invoked at all IS the unhealthy signal: the only reason to call this
script is that the real tool wasn't reachable. So every invocation also
timestamps state/telegram_fallback_used.json, which
watchdog_telegram_health.ps1 checks (alongside its existing debug-log grep)
to detect and heal a stale binding by restarting the orchestrator — without
this, the staleness can sit silently until a real reply attempt happens to
produce the matching log error, which may never occur.

Usage:
    python scripts/telegram_send.py "message text" [--chat-id 123456]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = REPO_ROOT / "scripts" / "scheduler.config.json"
TELEGRAM_ENV_FILE = Path(os.environ.get("USERPROFILE", "")) / ".claude" / "channels" / "telegram" / ".env"
FALLBACK_STATE_FILE = REPO_ROOT / "state" / "telegram_fallback_used.json"
MAX_CHARS = 4000


def _read_token() -> str | None:
    if not TELEGRAM_ENV_FILE.exists():
        return None
    for line in TELEGRAM_ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    return None


def _default_chat_id() -> str | None:
    try:
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        chat_id = config.get("alert_chat_id")
        return str(chat_id) if chat_id else None
    except Exception:  # noqa: BLE001
        return None


def _send_raw(token: str, chat_id: str, text: str) -> None:
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10)  # noqa: S310


def _record_fallback_used(reason: str) -> None:
    FALLBACK_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    FALLBACK_STATE_FILE.write_text(
        json.dumps({"last_used_at": datetime.now().isoformat(timespec="seconds"), "reason": reason}, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text", help="Message text to send")
    parser.add_argument("--chat-id", default=None, help="Telegram chat id (defaults to scheduler.config.json alert_chat_id)")
    parser.add_argument("--reason", default="mcp reply tool unavailable", help="Why the fallback was used (recorded for the watchdog)")
    args = parser.parse_args()

    token = _read_token()
    if not token:
        print("telegram_send: no bot token found at " + str(TELEGRAM_ENV_FILE), file=sys.stderr)
        return 1

    chat_id = args.chat_id or _default_chat_id()
    if not chat_id:
        print("telegram_send: no chat_id provided and none configured in scheduler.config.json", file=sys.stderr)
        return 1

    # Record the fallback use FIRST so the unhealthy signal is written even if
    # the send itself fails (both are worth the watchdog knowing about).
    _record_fallback_used(args.reason)

    chunks = [args.text[i:i + MAX_CHARS] for i in range(0, len(args.text), MAX_CHARS)] or [args.text]
    try:
        for chunk in chunks:
            _send_raw(token, chat_id, chunk)
    except Exception as exc:  # noqa: BLE001
        print(f"telegram_send: failed to deliver message: {exc}", file=sys.stderr)
        return 1

    print("telegram_send: delivered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
