#!/usr/bin/env python3
"""Direct-Bot-API fallback for sending a Telegram message.

Use this ONLY when the mcp__plugin_telegram_telegram__reply tool errors or
isn't available (e.g. "Tool ... not found in render-time tools") — a known
failure mode where an SSE stream reconnect silently drops the tool binding
without otherwise breaking the session. See memory: telegram_sse_reconnect_stale_tool.

Being invoked at all IS the unhealthy signal: the only reason to call this
script is that the real tool wasn't reachable. So every invocation also
timestamps state/telegram_fallback_used.json, which
watchdog_telegram_health.py checks (alongside its existing debug-log grep)
to detect and heal a stale binding by restarting the orchestrator — without
this, the staleness can sit silently until a real reply attempt happens to
produce the matching log error, which may never occur.

Every send goes through notify.py, so it is also recorded for the conversation
(a context note plus the Telegram message ids for reply-to lookups).

Usage:
    python scripts/telegram_send.py "message text" [--chat-id 123456] [--source agent]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

import notify
from paths import REPO_ROOT, alert_chat_id, get_env, secrets_dir, telegram_env_path

TELEGRAM_ENV_FILE = telegram_env_path()
FALLBACK_STATE_FILE = REPO_ROOT / "state" / "telegram_fallback_used.json"


def _read_token() -> str | None:
    # Process env, then ~/.config/scherbring-assistant/.env, repo .env, and
    # finally the Telegram plugin's own .env.
    return get_env("TELEGRAM_BOT_TOKEN", (TELEGRAM_ENV_FILE,))


def _default_chat_id() -> str | None:
    return alert_chat_id()




def _record_fallback_used(reason: str) -> None:
    FALLBACK_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    FALLBACK_STATE_FILE.write_text(
        json.dumps({"last_used_at": datetime.now().isoformat(timespec="seconds"), "reason": reason}, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text", help="Message text to send")
    parser.add_argument("--chat-id", default=None, help="Telegram chat id (defaults to TELEGRAM_ALERT_CHAT_ID)")
    parser.add_argument("--source", default="agent", help="Who is sending (recorded with the message)")
    parser.add_argument("--reason", default="mcp reply tool unavailable", help="Why the fallback was used (recorded for the watchdog)")
    args = parser.parse_args()

    token = _read_token()
    if not token:
        print(
            "telegram_send: no TELEGRAM_BOT_TOKEN found. Looked in the process env, "
            f"{secrets_dir() / '.env'}, {REPO_ROOT / '.env'} and {TELEGRAM_ENV_FILE}",
            file=sys.stderr,
        )
        return 1

    chat_id = args.chat_id or _default_chat_id()
    if not chat_id:
        print("telegram_send: no chat_id provided and none configured (set TELEGRAM_ALERT_CHAT_ID)", file=sys.stderr)
        return 1

    # Record the fallback use FIRST so the unhealthy signal is written even if
    # the send itself fails (both are worth the watchdog knowing about).
    _record_fallback_used(args.reason)

    res = notify.send(args.text, source=args.source, kind="agent", chat_id=chat_id, token=token)
    if not res.delivered:
        print(f"telegram_send: failed to deliver message: {res.error}", file=sys.stderr)
        return 1

    print("telegram_send: delivered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
