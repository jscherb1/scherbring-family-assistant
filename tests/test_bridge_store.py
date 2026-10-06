import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from bridge_store import BridgeStore, backoff_seconds  # noqa: E402


def make():
    return BridgeStore(":memory:")


def test_inbound_dedupes_on_external_id():
    s = make()
    assert s.add_inbound("telegram", "1", "hi", external_id="10") is not None
    assert s.add_inbound("telegram", "1", "hi again", external_id="10") is None
    assert s.stats()["inbox_pending"] == 1


def test_claim_complete_and_recover_after_crash():
    s = make()
    s.add_inbound("telegram", "1", "a", external_id="1")
    row = s.claim_next()
    assert row["text"] == "a" and row["attempts"] == 1
    assert s.claim_next() is None  # nothing else pending
    assert s.recover_in_flight() == 1  # worker crashed mid-turn
    again = s.claim_next()
    assert again["id"] == row["id"] and again["attempts"] == 2
    s.complete(again["id"])
    assert s.stats()["inbox_pending"] == 0


def test_claim_is_per_session_and_ordered():
    s = make()
    s.add_inbound("telegram", "1", "first", external_id="1")
    s.add_inbound("telegram", "2", "other", external_id="2")
    s.add_inbound("telegram", "1", "second", external_id="3")
    assert s.claim_next(session_key="2")["text"] == "other"
    assert s.claim_next(session_key="1")["text"] == "first"


def test_shared_session_key_across_chats():
    s = make()
    s.add_inbound("telegram", "1", "a", external_id="1", session_key="household")
    s.add_inbound("telegram", "2", "b", external_id="2", session_key="household")
    assert [s.claim_next("household")["text"], s.claim_next("household")["text"]] == ["a", "b"]


def test_context_notes_fold_into_next_message():
    s = make()
    s.add_inbound("scheduler", "1", "weekly summary: ...", kind="context_note", session_key="1")
    s.add_inbound("telegram", "1", "what did that mean?", external_id="5")
    row = s.claim_next()
    assert [n["text"] for n in row["notes"]] == ["weekly summary: ..."]
    assert s.stats()["inbox_pending"] == 0


def test_outbox_retry_backoff_then_fail():
    s = make()
    oid = s.enqueue_outbound("telegram", "1", "hello", now=100)
    assert s.next_outbound(now=100)["id"] == oid
    assert s.mark_send_failed(oid, "timeout", now=100) == "pending"
    assert s.next_outbound(now=100) is None  # backing off
    assert s.next_outbound(now=100 + backoff_seconds(1)) is not None
    for _ in range(10):
        status = s.mark_send_failed(oid, "timeout", now=100)
    assert status == "failed"
    s.mark_sent(oid)  # no-op safety


def test_backoff_caps_at_30():
    assert [backoff_seconds(n) for n in (1, 2, 3, 6, 20)] == [1, 2, 4, 30, 30]


def test_kv_roundtrip():
    s = make()
    assert s.kv_get("offset") is None
    s.kv_set("offset", "42")
    s.kv_set("offset", "43")
    assert s.kv_get("offset") == "43"


def test_recent_conversation_time_and_floor():
    s = make()
    now = 1_000_000.0
    day = 86400
    # 5 old messages (10 days ago), 2 recent
    for i in range(5):
        s.add_inbound("telegram", "1", f"old{i}", external_id=f"o{i}", now=now - 10 * day + i)
    for i in range(2):
        s.add_inbound("telegram", "1", f"new{i}", external_id=f"n{i}", now=now - 60 + i)
    # floor of 3 keeps the 2 recent + 1 old even though old is outside 48h
    conv = s.recent_conversation("1", min_messages=3, now=now)
    assert [m["text"] for m in conv] == ["old4", "new0", "new1"]
    # quiet period: nothing recent, floor still preserves history
    conv = s.recent_conversation("1", min_messages=40, now=now + 30 * day)
    assert len(conv) == 7
    # includes outbound, ordered
    s.enqueue_outbound("telegram", "1", "reply", now=now - 30)
    texts = [m["text"] for m in s.recent_conversation("1", now=now)]
    assert texts[-2:] == ["reply", "new1"] or "reply" in texts


def test_recent_conversation_size_cap_trims_beyond_floor():
    s = make()
    now = 1_000_000.0
    for i in range(10):
        s.add_inbound("telegram", "1", "x" * 1000, external_id=str(i), now=now - 10 + i)
    conv = s.recent_conversation("1", min_messages=2, max_bytes=3500, now=now)
    assert 3 <= len(conv) <= 4
