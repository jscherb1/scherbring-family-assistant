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


def test_notice_also_fires_for_message_stuck_in_processing(env):
    api, store, b = env
    store.add_inbound("telegram", "1", "hi", external_id="1", now=time.time() - 30)
    store.claim_next()  # worker took it, then died
    b.check_agent_notice(store)
    assert store.stats()["outbox_pending"] == 1


def test_notice_not_sent_for_non_telegram_rows(env):
    api, store, b = env
    store.add_inbound("terminal", "terminal", "hi", external_id="t1", session_key="1", now=time.time() - 30)
    b.check_agent_notice(store)
    assert store.stats()["outbox_pending"] == 0


def test_poll_failure_alerts_once_then_announces_recovery(env):
    api, store, b = env
    err = "getUpdates: Conflict: terminated by other getUpdates request"
    b.note_poll_result(store, err, now=1000)
    b.note_poll_result(store, err, now=1030)
    assert store.stats()["outbox_pending"] == 0  # not yet
    b.note_poll_result(store, err, now=1061)
    b.note_poll_result(store, err, now=1200)
    rows = [store.next_outbound("telegram")]
    assert store.stats()["outbox_pending"] == 1 and "another process is polling" in rows[0]["text"]
    store.mark_sent(rows[0]["id"])
    b.note_poll_result(store, None, now=1210)
    assert "recovered" in store.next_outbound("telegram")["text"]


def test_brief_poll_failures_do_not_alert(env):
    api, store, b = env
    b.note_poll_result(store, "getUpdates: timeout", now=1000)
    b.note_poll_result(store, None, now=1010)
    assert store.stats()["outbox_pending"] == 0


def test_poll_alert_is_not_retried_after_failed_send(env):
    api, store, b = env
    b.note_poll_result(store, "timed out", now=0)
    b.note_poll_result(store, "timed out", now=100)
    api.fail_send = True
    assert b.flush_outbox_once(store) == 0
    assert store.stats()["outbox_pending"] == 0  # abandoned, not rescheduled
    assert store.stats()["outbox_failed"] == 1


# ---------------------------------------------------------------- groups / dev chat

def group_upd(uid, chat, text, mid=1, **extra):
    msg = {"message_id": mid, "chat": {"id": chat, "type": "group"}, "from": {"username": "j", "id": 7}, "date": 1}
    if text is not None:
        msg["text"] = text
    msg.update(extra)
    return {"update_id": uid, "message": msg}


def test_group_message_is_enqueued_under_its_own_session(env):
    api, store, b = env
    b.allowed.add("-100")
    b.handle_update(store, group_upd(1, -100, "refactor the scheduler"))
    row = store.claim_next()
    assert row["chat_ref"] == "-100" and row["session_key"] == "-100" and row["text"] == "refactor the scheduler"
    assert parse_update(group_upd(2, -100, "x"))["chat_type"] == "group"


def test_status_command_with_botname_suffix_in_group(env):
    api, store, b = env
    b.allowed.add("-100")
    b.handle_update(store, group_upd(1, -100, "/status@scherbring_bot"))
    assert store.stats()["inbox_pending"] == 0  # handled by the bridge, not the agent
    assert api.sent() and "Agent worker" in api.sent()[0]


def test_group_migration_logs_new_id_and_enqueues_nothing(env, caplog):
    api, store, b = env
    b.allowed.add("-100")
    with caplog.at_level("WARNING", logger="bridge"):
        b.handle_update(store, group_upd(1, -100, None, migrate_to_chat_id=-1001234))
        b.handle_update(store, group_upd(2, -1001234, None, migrate_from_chat_id=-100))
    assert store.stats()["inbox_pending"] == 0
    warn = " ".join(r.getMessage() for r in caplog.records)
    assert "-1001234" in warn and "BRIDGE_ALLOWED_CHAT_IDS" in warn and "BRIDGE_DEV_CHAT_IDS" in warn
    assert "-1001234" not in b.allowed  # config is never rewritten automatically


def test_service_message_without_text_is_ignored(env):
    api, store, b = env
    b.allowed.add("-100")
    b.handle_update(store, group_upd(1, -100, None, new_chat_members=[{"id": 9}]))
    assert store.stats()["inbox_pending"] == 0


def test_unlisted_group_is_refused_with_its_id(env):
    api, store, b = env
    b.handle_update(store, group_upd(1, -555, "hi"))
    assert store.stats()["inbox_pending"] == 0 and "-555" in api.sent()[0]


def test_long_dev_turn_does_not_trigger_restart_notice_while_worker_alive(env):
    api, store, b = env
    store.add_inbound("telegram", "-100", "long dev task", external_id="1", now=time.time() - 600)
    store.claim_next()  # processing for 10 minutes
    store.kv_set("worker_heartbeat", str(time.time()))  # heartbeat runs on its own task, so it stays fresh
    b.check_agent_notice(store)
    assert store.stats()["outbox_pending"] == 0


def test_non_service_textless_messages_are_still_enqueued(env):
    api, store, b = env
    b.allowed.add("-100")
    for i, extra in enumerate(({"sticker": {"file_id": "s"}}, {"video_note": {"file_id": "v"}}, {"animation": {"file_id": "a"}},
                               {"location": {"latitude": 1, "longitude": 2}}, {"contact": {"phone_number": "1"}}), start=1):
        b.handle_update(store, group_upd(i, -100, None, mid=i, **extra))
    assert store.stats()["inbox_pending"] == 5  # the rule: never leave a message unanswered
    for key in ("pinned_message", "left_chat_member", "new_chat_title", "group_chat_created"):
        assert parse_update(group_upd(9, -100, None, **{key: {}}))["service"] is True


def test_poll_outage_alert_skips_dev_group_even_with_negative_id(env, monkeypatch):
    import paths
    import telegram_bridge

    fake = lambda name, *a, **k: {"BRIDGE_DEV_CHAT_IDS": "-5468858630", "BRIDGE_ALLOWED_CHAT_IDS": "8150343517,-5468858630"}.get(name)  # noqa: E731
    monkeypatch.setattr(paths, "get_env", fake)
    monkeypatch.setattr(telegram_bridge, "get_env", fake)
    api, store, _ = env
    b = Bridge(api, store.path, {"8150343517", "-5468858630"})  # sorted() would put the group first
    assert b._alert_chat() == "8150343517"
    b.note_poll_result(store, "boom", now=0)
    b.note_poll_result(store, "boom", now=100)
    assert store.next_outbound("telegram")["chat_ref"] == "8150343517"
    # No household id configured at all: the dev group is still never used.
    b2 = Bridge(api, store.path, {"-5468858630"})
    assert b2._alert_chat() is None
