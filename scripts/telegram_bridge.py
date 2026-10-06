#!/usr/bin/env python3
"""Telegram bridge: owns Bot API polling and delivery, independent of the agent.

Inbound: long-poll getUpdates -> persist to the inbox (bridge_store) -> only then
advance the offset. A crash re-delivers; the inbox dedupes on update_id.
Outbound: drain the outbox with retry/backoff so replies survive API outages.

It also does what must keep working while the agent is down: instant 👀 ack,
/status, and a "message saved, agent restarting" notice.

Usage:
    python scripts/telegram_bridge.py [--token-env TELEGRAM_TEST_BOT_TOKEN]

Env (process env or ~/.config/scherbring-assistant/.env):
    <token-env>                 bot token (default TELEGRAM_TEST_BOT_TOKEN)
    BRIDGE_ALLOWED_CHAT_IDS     comma-separated chat ids; empty = pairing mode
                                (nobody is enqueued; the bot replies with their chat id)
    BRIDGE_SESSION_MAP          optional JSON {"chat_id": "session_key"} to share sessions
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bridge_store import BridgeStore  # noqa: E402
from paths import REPO_ROOT, get_env, telegram_env_path  # noqa: E402

log = logging.getLogger("bridge")

CHANNEL = "telegram"
MAX_CHARS = 4000
POLL_TIMEOUT = 25  # long-poll seconds
WORKER_STALE_S = 15  # worker heartbeat older than this = "agent restarting"
NOTICE_AFTER_S = 10  # unanswered this long + stale worker -> notice
POLL_ALERT_AFTER_S = 60  # polling failing this long -> tell the user (sending still works)
ATTACH_DIR = REPO_ROOT / "state" / "attachments"


class TelegramError(Exception):
    pass


class TelegramAPI:
    def __init__(self, token: str):
        self._base = f"https://api.telegram.org/bot{token}/"
        self._file_base = f"https://api.telegram.org/file/bot{token}/"

    def call(self, method: str, params: dict | None = None, timeout: float = 15) -> Any:
        data = urllib.parse.urlencode(
            {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in (params or {}).items()}
        ).encode()
        req = urllib.request.Request(self._base + method, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                body = json.load(resp)
        except urllib.error.HTTPError as e:
            try:
                body = json.load(e)
            except Exception:  # noqa: BLE001
                raise TelegramError(f"{method}: HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise TelegramError(f"{method}: {e}") from e
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description')}")
        return body["result"]

    def download(self, file_id: str, dest_dir: Path) -> Path:
        info = self.call("getFile", {"file_id": file_id})
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{int(time.time())}_{Path(info['file_path']).name}"
        with urllib.request.urlopen(self._file_base + info["file_path"], timeout=60) as r:  # noqa: S310
            dest.write_bytes(r.read())
        return dest


def chunk_text(text: str, limit: int = MAX_CHARS) -> list[str]:
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


def parse_update(update: dict) -> dict | None:
    """Reduce a Telegram update to what we store; None if not a user message."""
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return None
    frm = msg.get("from") or {}
    attachments = []
    if msg.get("photo"):
        attachments.append({"kind": "photo", "file_id": msg["photo"][-1]["file_id"]})
    for kind in ("document", "voice", "audio", "video"):
        if msg.get(kind):
            attachments.append({"kind": kind, "file_id": msg[kind]["file_id"], "name": msg[kind].get("file_name")})
    return {
        "chat_id": str(msg["chat"]["id"]),
        "message_id": msg["message_id"],
        "text": msg.get("text") or msg.get("caption") or "",
        "user": frm.get("username") or frm.get("first_name") or "",
        "user_id": frm.get("id"),
        "date": msg.get("date"),
        "edited": "edited_message" in update,
        "attachments": attachments,
    }


class Bridge:
    def __init__(self, api: Any, store_path: Path | str, allowed: set[str], session_map: dict[str, str] | None = None):
        self.api = api
        self.store_path = store_path
        self.allowed = allowed
        self.session_map = session_map or {}
        self.stop = threading.Event()
        self._notified_for: int | None = None  # inbox id we already sent the restart notice for
        self._poll_fail_since: float | None = None
        self._poll_alerted = False

    # ---- inbound ----
    def handle_update(self, store: BridgeStore, update: dict) -> None:
        parsed = parse_update(update)
        if parsed is None:
            return
        chat = parsed["chat_id"]
        if chat not in self.allowed:
            # Pairing aid: tell the sender their chat id; never enqueue.
            log.warning("message from non-allowlisted chat %s", chat)
            self._safe(lambda: self.api.call("sendMessage", {"chat_id": chat, "text": f"Not authorized. Your chat id is {chat}. Ask the owner to add it to BRIDGE_ALLOWED_CHAT_IDS."}))
            return
        text = parsed["text"].strip()
        if text.split("@")[0] == "/status":
            self._safe(lambda: self.api.call("sendMessage", {"chat_id": chat, "text": self.status_text(store)}))
            return
        # Download attachments so the worker never needs the Bot API file endpoint.
        for att in parsed["attachments"]:
            try:
                att["path"] = str(self.api.download(att["file_id"], ATTACH_DIR))
            except Exception as e:  # noqa: BLE001
                log.warning("attachment download failed: %s", e)
        store.add_inbound(
            CHANNEL,
            chat,
            parsed["text"],
            external_id=str(update["update_id"]),
            session_key=self.session_map.get(chat, chat),
            payload={k: parsed[k] for k in ("message_id", "user", "user_id", "date", "edited", "attachments")},
        )
        # Instant ack (best effort); the durable write above is what matters.
        self._safe(lambda: self.api.call("setMessageReaction", {"chat_id": chat, "message_id": parsed["message_id"], "reaction": [{"type": "emoji", "emoji": "👀"}]}))
        self._safe(lambda: self.api.call("sendChatAction", {"chat_id": chat, "action": "typing"}))

    def poll_once(self, store: BridgeStore, timeout: int = POLL_TIMEOUT) -> int:
        offset = int(store.kv_get("tg_offset", "0") or 0)
        updates = self.api.call("getUpdates", {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "edited_message"]}, timeout=timeout + 10)
        for upd in updates:
            self.handle_update(store, upd)
            # Advance only after the row is committed.
            store.kv_set("tg_offset", str(upd["update_id"] + 1))
        store.kv_set("last_poll_ok", str(time.time()))
        return len(updates)

    # ---- outbound ----
    def flush_outbox_once(self, store: BridgeStore) -> int:
        sent = 0
        while True:
            row = store.next_outbound(CHANNEL)
            if row is None:
                return sent
            try:
                for part in chunk_text(row["text"]):
                    self.api.call("sendMessage", {"chat_id": row["chat_ref"], "text": part})
                store.mark_sent(row["id"])
                sent += 1
            except TelegramError as e:
                status = store.mark_send_failed(row["id"], str(e))
                log.warning("send failed (%s): %s", status, e)
                return sent

    # ---- agent-down notice ----
    def check_agent_notice(self, store: BridgeStore) -> None:
        stats = store.stats()
        age = stats["oldest_pending_age_s"]
        if age is None:
            self._notified_for = None
            return
        hb = float(store.kv_get("worker_heartbeat", "0") or 0)
        worker_down = time.time() - hb > WORKER_STALE_S
        if age >= NOTICE_AFTER_S and worker_down:
            oldest = store._conn.execute("SELECT id, chat_ref FROM inbox WHERE status IN ('pending','processing') AND kind='message' AND channel=? ORDER BY id LIMIT 1", (CHANNEL,)).fetchone()
            if oldest and self._notified_for != oldest["id"]:
                self._notified_for = oldest["id"]
                store.enqueue_outbound(CHANNEL, oldest["chat_ref"], "⚠️ The agent is restarting. Your message is saved and will be answered as soon as it's back.")

    def status_text(self, store: BridgeStore) -> str:
        s = store.stats()
        hb = float(store.kv_get("worker_heartbeat", "0") or 0)
        worker = "up" if time.time() - hb <= WORKER_STALE_S else "DOWN"
        lines = [f"Bridge: up", f"Agent worker: {worker}", f"Queued messages: {s['inbox_pending']}"]
        if s["oldest_pending_age_s"]:
            lines.append(f"Oldest waiting: {int(s['oldest_pending_age_s'])}s")
        if s["outbox_failed"]:
            lines.append(f"Undeliverable replies: {s['outbox_failed']}")
        return "\n".join(lines)

    def note_poll_result(self, store: BridgeStore, error: str | None, now: float | None = None) -> None:
        """Alert (once) when polling has been failing for a while, and again when it recovers.
        Sending uses a different endpoint, so the alert gets through even when polling is blocked."""
        ts = now if now is not None else time.time()
        chat = sorted(self.allowed)[0] if self.allowed else None
        if error is None:
            if self._poll_alerted and chat:
                store.enqueue_outbound(CHANNEL, chat, "✅ Telegram polling recovered; messages are being received again.")
            self._poll_fail_since, self._poll_alerted = None, False
            return
        if self._poll_fail_since is None:
            self._poll_fail_since = ts
        if ts - self._poll_fail_since >= POLL_ALERT_AFTER_S and not self._poll_alerted and chat:
            self._poll_alerted = True
            if "Conflict" in error:
                why = "another process is polling this bot (for example a Claude session with the Telegram plugin enabled)"
            else:
                why = f"Telegram can't be reached ({error[:120]})"
            store.enqueue_outbound(CHANNEL, chat, f"⚠️ I'm not receiving your messages right now: {why}. Anything you send may be delayed until this clears.")

    # ---- run loops ----
    @staticmethod
    def _safe(fn) -> None:
        try:
            fn()
        except TelegramError as e:
            log.debug("best-effort call failed: %s", e)

    def _poll_loop(self) -> None:
        store = BridgeStore(self.store_path)
        delay = 1.0
        while not self.stop.is_set():
            try:
                self.poll_once(store)
                self.note_poll_result(store, None)
                delay = 1.0
            except TelegramError as e:
                self.note_poll_result(store, str(e))
                log.warning("poll failed: %s (retry in %.0fs)", e, delay)
                self.stop.wait(delay)
                delay = min(30.0, delay * 2)

    def _send_loop(self) -> None:
        store = BridgeStore(self.store_path)
        while not self.stop.is_set():
            self.flush_outbox_once(store)
            self.check_agent_notice(store)
            self.stop.wait(0.5)

    def run(self) -> None:
        threads = [threading.Thread(target=f, name=n, daemon=True) for n, f in (("poll", self._poll_loop), ("send", self._send_loop))]
        for t in threads:
            t.start()
        hb_store = BridgeStore(self.store_path)
        while not self.stop.is_set():
            if not all(t.is_alive() for t in threads):
                log.error("a bridge thread died; exiting so systemd restarts us")
                sys.exit(1)
            hb_store.kv_set("bridge_heartbeat", str(time.time()))  # process alive; polling health is last_poll_ok
            self.stop.wait(1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--token-env", default="TELEGRAM_TEST_BOT_TOKEN")
    ap.add_argument("--db", default=str(REPO_ROOT / "state" / "inbox.db"))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # Production token also lives in the old plugin's .env; same lookup order as telegram_send.py.
    token = get_env(args.token_env, (telegram_env_path(),))
    if not token:
        print(f"telegram_bridge: {args.token_env} not set", file=sys.stderr)
        return 1
    allowed = {c.strip() for c in (get_env("BRIDGE_ALLOWED_CHAT_IDS") or "").split(",") if c.strip()}
    session_map = json.loads(get_env("BRIDGE_SESSION_MAP") or "{}")
    if not allowed:
        log.warning("BRIDGE_ALLOWED_CHAT_IDS empty: pairing mode (no messages will be enqueued)")
    bridge = Bridge(TelegramAPI(token), args.db, allowed, session_map)
    signal.signal(signal.SIGTERM, lambda *_: bridge.stop.set())
    signal.signal(signal.SIGINT, lambda *_: bridge.stop.set())
    bridge.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
