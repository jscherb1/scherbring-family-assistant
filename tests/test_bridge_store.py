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


def test_concurrent_first_open_does_not_lock(tmp_path):
    import threading

    errors = []

    def open_db():
        try:
            BridgeStore(tmp_path / "race.db").close()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=open_db) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


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


def test_health_snapshot():
    s = make()
    now = 1000.0
    snap = s.health_snapshot(now=now)
    assert snap["bridge_up"] is False and snap["worker_up"] is False
    s.kv_set("bridge_heartbeat", "995")
    s.kv_set("worker_heartbeat", "900")
    s.kv_set("paused", "1")
    snap = s.health_snapshot(now=now)
    assert snap["bridge_up"] is True and snap["worker_up"] is False and snap["paused"] is True
    assert snap["worker_heartbeat_age_s"] == 100.0 and snap["inbox_pending"] == 0


def test_health_distinguishes_alive_from_polling():
    s = make()
    s.kv_set("bridge_heartbeat", "999")
    s.kv_set("last_poll_ok", "800")
    snap = s.health_snapshot(now=1000.0)
    assert snap["bridge_up"] is True and snap["polling_ok"] is False


def test_notes_survive_redelivery_and_close_on_complete():
    s = make()
    s.add_inbound("scheduler", "1", "note A", kind="context_note")
    s.add_inbound("telegram", "1", "q", external_id="1")
    first = s.claim_next()
    assert [n["text"] for n in first["notes"]] == ["note A"]
    s.recover_in_flight()  # worker crashed mid-turn
    again = s.claim_next()
    assert [n["text"] for n in again["notes"]] == ["note A"]  # retry still sees the note
    s.complete(again["id"])
    s.add_inbound("telegram", "1", "q2", external_id="2")
    assert s.claim_next()["notes"] == []  # consumed exactly once


def test_note_fold_is_capped_but_all_closed():
    from bridge_store import MAX_FOLDED_NOTES

    s = make()
    for i in range(MAX_FOLDED_NOTES + 5):
        s.add_inbound("scheduler", "1", f"n{i}", kind="context_note")
    s.add_inbound("telegram", "1", "q", external_id="1")
    row = s.claim_next()
    assert len(row["notes"]) == MAX_FOLDED_NOTES and row["notes"][-1]["text"] == f"n{MAX_FOLDED_NOTES + 4}"
    s.complete(row["id"])
    assert s._conn.execute("SELECT COUNT(*) FROM inbox WHERE kind='context_note' AND status!='done'").fetchone()[0] == 0


def test_record_context_note_helper(tmp_path, monkeypatch):
    from bridge_store import record_context_note

    monkeypatch.setenv("BRIDGE_SESSION_MAP", '{"2": "household"}')
    db = tmp_path / "i.db"
    record_context_note("2", "weekly summary", "scheduler", db=db)
    record_context_note("3", "other", "scheduler", db=db)
    s = BridgeStore(db)
    s.add_inbound("telegram", "2", "q", external_id="1", session_key="household")
    row = s.claim_next("household")
    assert [n["text"] for n in row["notes"]] == ["weekly summary"]
    record_context_note("1", "x", "scheduler", db="/nonexistent-dir/x/i.db")  # never raises


def test_snapshot_ignores_unused_claude_ai_connectors(monkeypatch):
    import json

    import bridge_store
    s = make()
    s.kv_set("mcp_down", json.dumps(["claude.ai Unused", "claude.ai Monarch Money", "todoist"]))
    monkeypatch.setattr(bridge_store, "IGNORED_MCP", {"claude.ai Unused"})
    # The official Monarch connector is the finance agent's fallback, so its outages are reported.
    assert s.health_snapshot()["mcp_down"] == ["claude.ai Monarch Money", "todoist"]
