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
    BRIDGE_DEV_CHAT_IDS         optional comma-separated subset of the allowed ids that are dev chats (groups);
                                read by the worker, not here. Each chat is its own session, so no mapping needed
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
from paths import REPO_ROOT, dev_chat_ids, get_env, household_chat_id, telegram_env_path  # noqa: E402

log = logging.getLogger("bridge")

CHANNEL = "telegram"
MAX_CHARS = 4000
POLL_TIMEOUT = 25  # long-poll seconds
WORKER_STALE_S = 15  # worker heartbeat older than this = "agent restarting"
NOTICE_AFTER_S = 10  # unanswered this long + stale worker -> notice
POLL_ALERT_AFTER_S = 60  # polling failing this long -> tell the user (sending still works)
ATTACH_DIR = REPO_ROOT / "state" / "attachments"


# Telegram service messages: group events with nothing for the agent to answer. Anything else without
# text (stickers, video notes, animations, locations, contacts) is a real message and is still enqueued.
SERVICE_KEYS = (
    "new_chat_members", "left_chat_member", "new_chat_title", "new_chat_photo", "delete_chat_photo",
    "group_chat_created", "supergroup_chat_created", "channel_chat_created", "message_auto_delete_timer_changed",
    "migrate_to_chat_id", "migrate_from_chat_id", "pinned_message", "forum_topic_created", "forum_topic_edited",
    "forum_topic_closed", "forum_topic_reopened", "video_chat_started", "video_chat_ended",
    "video_chat_scheduled", "video_chat_participants_invited", "boost_added",
)


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
    reply_to = None
    if msg.get("reply_to_message"):
        r = msg["reply_to_message"]
        reply_to = {
            "message_id": r.get("message_id"),
            "text": (r.get("text") or r.get("caption") or "")[:4000],
            "from_bot": bool((r.get("from") or {}).get("is_bot")),
        }
    return {
        "chat_id": str(msg["chat"]["id"]),
        "chat_type": msg["chat"].get("type", ""),  # private | group | supergroup
        "migrate_to_chat_id": msg.get("migrate_to_chat_id"),  # service message: this group became a supergroup
        "migrate_from_chat_id": msg.get("migrate_from_chat_id"),
        "service": any(k in msg for k in SERVICE_KEYS),
        "message_id": msg["message_id"],
        "text": msg.get("text") or msg.get("caption") or "",
        "user": frm.get("username") or frm.get("first_name") or "",
        "user_id": frm.get("id"),
        "date": msg.get("date"),
        "edited": "edited_message" in update,
        "attachments": attachments,
        "reply_to": reply_to,
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
        self._send_once: set[int] = set()  # outbox ids that must never be retried

    # ---- inbound ----
    def handle_update(self, store: BridgeStore, update: dict) -> None:
        parsed = parse_update(update)
        if parsed is None:
            return
        chat = parsed["chat_id"]
        if parsed["migrate_to_chat_id"]:
            # Telegram gives a basic group a NEW id when it becomes a supergroup; the old id stops working.
            # Config is never rewritten automatically: say exactly what to change.
            new = parsed["migrate_to_chat_id"]
            log.warning("chat %s was upgraded to a supergroup with NEW chat id %s. Replace %s with %s in "
                        "BRIDGE_ALLOWED_CHAT_IDS (and BRIDGE_DEV_CHAT_IDS if it is the dev chat), then restart "
                        "the bridge and agent worker. Messages from the new id are rejected until you do.", chat, new, chat, new)
            return
        if parsed["service"]:
            return  # group events (member joined, title changed, pinned...) carry nothing to answer
        if chat not in self.allowed:
            # Pairing aid: tell the sender their chat id; never enqueue.
            log.warning("message from non-allowlisted chat %s", chat)
            self._safe(lambda: self.api.call("sendMessage", {"chat_id": chat, "text": f"Not authorized. Your chat id is {chat}. Ask the owner to add it to BRIDGE_ALLOWED_CHAT_IDS."}))
            return
        text = parsed["text"].strip()
        if (text.split() or [""])[0].split("@")[0] == "/status":  # also "/status@botname" as groups send it
            status = self.status_text(store)

            def send_status() -> None:
                sent = self.api.call("sendMessage", {"chat_id": chat, "text": status})
                ids = [sent["message_id"]] if isinstance(sent, dict) and "message_id" in sent else []
                store.record_sent(CHANNEL, chat, status, ids, source="bridge", kind="notice", note=False)

            self._safe(send_status)
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
            payload={k: parsed[k] for k in ("message_id", "user", "user_id", "date", "edited", "attachments", "reply_to")},
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
                ids = []
                for part in chunk_text(row["text"]):
                    sent_msg = self.api.call("sendMessage", {"chat_id": row["chat_ref"], "text": part})
                    if isinstance(sent_msg, dict) and sent_msg.get("message_id") is not None:
                        ids.append(sent_msg["message_id"])
                store.mark_sent(row["id"], tg_message_ids=ids)
                sent += 1
            except TelegramError as e:
                if row["id"] in self._send_once:
                    # Status alerts: a timeout may mean Telegram delivered it anyway; retrying duplicates it.
                    self._send_once.discard(row["id"])
                    status = store.mark_send_abandoned(row["id"], str(e))
                    log.warning("send abandoned (no retry): %s", e)
                    continue
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
                store.enqueue_outbound(CHANNEL, oldest["chat_ref"], "⚠️ The agent is restarting. Your message is saved and will be answered as soon as it's back.", source="bridge", kind="notice")

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

    def _alert_chat(self) -> str | None:
        """Where outage alerts go: the explicit alert chat, else a household chat. Never a dev group
        (negative ids sort first, so "the first allowed id" would pick one)."""
        dev = dev_chat_ids()
        explicit = (get_env("TELEGRAM_ALERT_CHAT_ID") or "").strip()
        if explicit and explicit not in dev and explicit in self.allowed:
            return explicit
        household = household_chat_id()
        if household and household in self.allowed:
            return household
        rest = sorted((c for c in self.allowed if c not in dev), key=lambda c: (c.startswith("-"), c))
        return rest[0] if rest else None

    def note_poll_result(self, store: BridgeStore, error: str | None, now: float | None = None) -> None:
        """Alert (once) when polling has been failing for a while, and again when it recovers.
        Sending uses a different endpoint, so the alert gets through even when polling is blocked."""
        ts = now if now is not None else time.time()
        chat = self._alert_chat()
        if error is None:
            if self._poll_alerted and chat:
                self._send_once.add(store.enqueue_outbound(CHANNEL, chat, "✅ Telegram polling recovered; messages are being received again.", source="bridge", kind="alert", note=True))
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
            self._send_once.add(store.enqueue_outbound(CHANNEL, chat, f"⚠️ I'm not receiving your messages right now: {why}. Anything you send may be delayed until this clears.", source="bridge", kind="alert", note=True))

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

    # Production token may still live in the old plugin's .env (until rotated); same lookup order as telegram_send.py.
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
