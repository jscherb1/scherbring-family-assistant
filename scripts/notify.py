#!/usr/bin/env python3
"""The one way anything outside a conversation turn sends the user a Telegram message.

Scheduled-task results, alerts (credentials, system health, task failures, watchdogs, backups) and
agent-run `telegram_send.py` all come through `send()`. It delivers straight to the Bot API, so it
works while the bridge or worker is down, then records what landed in the bridge store:
  - a sent outbox row with the Telegram message ids (so a reply-to can be traced back), and
  - a context note, so the live conversation sees it on the user's next message.

Recording is best effort and never raises: an alert must not fail because bookkeeping did.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bridge_store import DEFAULT_DB, BridgeStore  # noqa: E402
from paths import alert_chat_id, get_env, telegram_env_path  # noqa: E402

MAX_CHARS = 4000

Sender = Callable[[str, str, str], int]


@dataclass
class Result:
    message_ids: list[int] = field(default_factory=list)
    delivered: bool = False  # every chunk accepted
    error: str = ""


def bot_token() -> str | None:
    return get_env("TELEGRAM_BOT_TOKEN", (telegram_env_path(),))


def send_raw(token: str, chat_id: str, text: str) -> int:
    """One sendMessage call; returns the Telegram message id."""
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        body = json.load(resp)
    return int(body["result"]["message_id"])


def chunk_text(text: str, limit: int = MAX_CHARS) -> list[str]:
    """Split on newlines where possible (same rule as the bridge)."""
    if len(text) <= limit:
        return [text]
    chunks, rest = [], text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    if rest:
        chunks.append(rest)
    return chunks


def send(text: str, *, source: str, kind: str = "alert", chat_id: str | None = None, note: bool = True,
         db: Path | str = DEFAULT_DB, token: str | None = None, sender: Sender | None = None) -> Result:
    """Deliver `text` and record it. `source` names the sender ("credential-check",
    "scheduled:weekly-spending-summary"); `kind` is alert | scheduled | notice."""
    res = Result()
    chat = str(chat_id or alert_chat_id() or "")
    tok = token or bot_token()
    if not text or not chat or not tok:
        res.error = "nothing to send" if not text else "no Telegram chat id or bot token configured"
        return res
    sender = sender or send_raw
    sent_parts: list[str] = []
    for part in chunk_text(text):
        try:
            res.message_ids.append(sender(tok, chat, part))
        except Exception as exc:  # noqa: BLE001
            res.error = f"{type(exc).__name__}: {exc}"
            break
        sent_parts.append(part)
    res.delivered = not res.error
    if sent_parts:  # record whatever reached the user, even if a later chunk failed
        landed = "\n".join(sent_parts) + ("" if res.delivered else "\n[...the rest failed to send]")
        try:
            store = BridgeStore(db)
            try:
                store.record_sent("telegram", chat, landed, res.message_ids, source=source, kind=kind, note=note)
            finally:
                store.close()
        except Exception as exc:  # noqa: BLE001
            print(f"notify: sent but could not record: {exc}", file=sys.stderr)
    return res
