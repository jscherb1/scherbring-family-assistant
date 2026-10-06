import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pytest  # noqa: E402

from bridge_store import BridgeStore  # noqa: E402
from telegram_bridge import Bridge, TelegramError, chunk_text, parse_update  # noqa: E402


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.updates = []
        self.fail_send = False

    def call(self, method, params=None, timeout=15):
        if method == "sendMessage" and self.fail_send:
            raise TelegramError("sendMessage: boom")
        self.calls.append((method, params))
        if method == "getUpdates":
            offset = params["offset"]
            return [u for u in self.updates if u["update_id"] >= offset]
        return True

    def download(self, file_id, dest_dir):
        return Path("/tmp/x")

    def sent(self):
        return [p["text"] for m, p in self.calls if m == "sendMessage"]


def upd(uid, chat, text, mid=1):
    return {"update_id": uid, "message": {"message_id": mid, "chat": {"id": chat}, "from": {"username": "j"}, "text": text, "date": 1}}


@pytest.fixture
def env(tmp_path):
    api = FakeAPI()
    store = BridgeStore(tmp_path / "i.db")
    return api, store, Bridge(api, tmp_path / "i.db", {"1"})


def test_message_persisted_before_offset_and_acked(env):
    api, store, b = env
    api.updates = [upd(10, 1, "hello")]
    assert b.poll_once(store, timeout=0) == 1
    assert store.kv_get("tg_offset") == "11"
    row = store.claim_next()
    assert row["text"] == "hello" and row["external_id"] == "10"
    methods = [m for m, _ in api.calls]
    assert "setMessageReaction" in methods and "sendChatAction" in methods


def test_redelivery_after_crash_is_deduped(env):
    api, store, b = env
    api.updates = [upd(10, 1, "hello")]
    b.poll_once(store, timeout=0)
    store.kv_set("tg_offset", "10")  # simulate crash before offset persisted
    b.poll_once(store, timeout=0)
    assert store.stats()["inbox_pending"] == 1


def test_unauthorized_chat_gets_chat_id_and_is_not_enqueued(env):
    api, store, b = env
    api.updates = [upd(1, 999, "let me in")]
    b.poll_once(store, timeout=0)
    assert store.stats()["inbox_pending"] == 0
    assert "999" in api.sent()[0]


def test_status_command_answered_by_bridge(env):
    api, store, b = env
    api.updates = [upd(1, 1, "/status")]
    b.poll_once(store, timeout=0)
    assert store.stats()["inbox_pending"] == 0
    assert "Agent worker: DOWN" in api.sent()[0]
    store.kv_set("worker_heartbeat", str(time.time()))
    assert "Agent worker: up" in b.status_text(store)


def test_outbox_flush_and_retry(env):
    api, store, b = env
    store.enqueue_outbound("telegram", "1", "answer")
    api.fail_send = True
    assert b.flush_outbox_once(store) == 0
    assert store.stats()["outbox_pending"] == 1
    api.fail_send = False
    store._conn.execute("UPDATE outbox SET next_attempt_at=0")
    assert b.flush_outbox_once(store) == 1
    assert api.sent() == ["answer"]


def test_agent_down_notice_sent_once(env):
    api, store, b = env
    store.add_inbound("telegram", "1", "hi", external_id="1", now=time.time() - 30)
    b.check_agent_notice(store)
    b.check_agent_notice(store)
    assert store.stats()["outbox_pending"] == 1  # only one notice
    # healthy worker -> no notice
    api2, store2, b2 = FakeAPI(), BridgeStore(":memory:"), None
    b2 = Bridge(api2, ":memory:", {"1"})
    store2.add_inbound("telegram", "1", "hi", external_id="1", now=time.time() - 30)
    store2.kv_set("worker_heartbeat", str(time.time()))
    b2.check_agent_notice(store2)
    assert store2.stats()["outbox_pending"] == 0


def test_chunking_and_parse():
    text = ("a" * 100 + "\n") * 100
    parts = chunk_text(text, 1000)
    assert all(len(p) <= 1000 for p in parts) and "".join(parts).replace("\n", "") == text.replace("\n", "")
    p = parse_update({"update_id": 1, "message": {"message_id": 2, "chat": {"id": 5}, "photo": [{"file_id": "s"}, {"file_id": "big"}], "caption": "pic"}})
    assert p["text"] == "pic" and p["attachments"][0]["file_id"] == "big"
    assert parse_update({"update_id": 2, "callback_query": {}}) is None
