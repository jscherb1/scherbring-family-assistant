#!/usr/bin/env python3
"""CLI over notify.py: send a Telegram message directly through the Bot API.

For agents and scripts that need to message the user outside a normal reply (a normal reply is just
the agent's final message). Every send goes through notify.py, so it is also recorded for the
conversation (a context note plus the Telegram message ids for reply-to lookups).

Usage:
    python scripts/telegram_send.py "message text" [--chat-id 123456] [--source agent]
"""

from __future__ import annotations

import argparse
import sys

import notify
from paths import REPO_ROOT, alert_chat_id, get_env, secrets_dir, telegram_env_path

TELEGRAM_ENV_FILE = telegram_env_path()


def _read_token() -> str | None:
    # Process env, then ~/.config/scherbring-assistant/.env, repo .env, and
    # finally the old Telegram plugin's .env (kept until the token is rotated).
    return get_env("TELEGRAM_BOT_TOKEN", (TELEGRAM_ENV_FILE,))


def _default_chat_id() -> str | None:
    return alert_chat_id()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text", help="Message text to send")
    parser.add_argument("--chat-id", default=None, help="Telegram chat id (defaults to TELEGRAM_ALERT_CHAT_ID)")
    parser.add_argument("--source", default="agent", help="Who is sending (recorded with the message)")
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

    res = notify.send(args.text, source=args.source, kind="agent", chat_id=chat_id, token=token)
    if not res.delivered:
        print(f"telegram_send: failed to deliver message: {res.error}", file=sys.stderr)
        return 1

    print("telegram_send: delivered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
