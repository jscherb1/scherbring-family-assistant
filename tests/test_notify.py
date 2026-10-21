import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import notify  # noqa: E402
from agent_worker import format_seed, format_turn_prompt, resolve_reply_to  # noqa: E402
from bridge_store import BridgeStore  # noqa: E402
from telegram_bridge import parse_update  # noqa: E402


def counting_sender(fail_on=None):
    calls = []

    def send(token, chat, text):
        calls.append(text)
        if fail_on is not None and len(calls) == fail_on:
            raise OSError("boom")
        return 100 + len(calls)

    return send, calls


def test_send_records_outbox_row_ids_and_note(tmp_path):
    db = tmp_path / "i.db"
    sender, calls = counting_sender()
    res = notify.send("Drive login expired", source="credential-check", chat_id="42", token="t", db=db, sender=sender)
    assert res.delivered and res.message_ids == [101]
    s = BridgeStore(db)
    hit = s.find_sent("42", 101)
    assert hit["text"] == "Drive login expired" and hit["source"] == "credential-check" and hit["kind"] == "alert"
    assert s.next_outbound() is None  # recorded as already sent; the bridge never re-sends it
    note = s._conn.execute("SELECT text, session_key FROM inbox WHERE kind='context_note'").fetchone()
    assert "[credential-check] sent you: Drive login expired" == note["text"] and note["session_key"] == "42"


def test_partial_delivery_records_what_landed(tmp_path):
    db = tmp_path / "i.db"
    sender, calls = counting_sender(fail_on=2)
    text = ("a" * 3000 + "\n") * 2  # two chunks
    res = notify.send(text, source="scheduled:weekly", kind="scheduled", chat_id="42", token="t", db=db, sender=sender)
    assert not res.delivered and res.message_ids == [101] and "boom" in res.error
    hit = BridgeStore(db).find_sent("42", 101)
    assert hit["text"].startswith("a" * 3000) and "rest failed to send" in hit["text"]


def test_nothing_sent_records_nothing_and_never_raises(tmp_path):
    db = tmp_path / "i.db"
    sender, _ = counting_sender(fail_on=1)
    res = notify.send("x", source="backup", chat_id="42", token="t", db=db, sender=sender)
    assert not res.delivered and res.message_ids == []
    assert BridgeStore(db)._conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    # Recording failure (unwritable db) still returns the delivery result.
    ok, _ = counting_sender()
    res = notify.send("x", source="backup", chat_id="42", token="t", db="/nonexistent-dir/x/i.db", sender=ok)
    assert res.delivered


def test_missing_config_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(notify, "alert_chat_id", lambda: None)
    res = notify.send("x", source="backup", token="t", db=tmp_path / "i.db", sender=counting_sender()[0])
    assert not res.delivered and "chat id" in res.error


def test_parse_update_keeps_reply_to():
    u = {"update_id": 1, "message": {"message_id": 9, "chat": {"id": 42}, "from": {"username": "j"}, "text": "done, re-logged in",
                                     "date": 1, "reply_to_message": {"message_id": 101, "text": "Drive login expired", "from": {"is_bot": True}}}}
    p = parse_update(u)
    assert p["reply_to"] == {"message_id": 101, "text": "Drive login expired", "from_bot": True}
    assert parse_update({"update_id": 2, "message": {"message_id": 1, "chat": {"id": 1}, "text": "hi"}})["reply_to"] is None


def test_reply_to_resolves_to_full_recorded_message(tmp_path):
    db = tmp_path / "i.db"
    notify.send("Full alert text with details", source="credential-check", chat_id="42", token="t", db=db,
                sender=counting_sender()[0])
    store = BridgeStore(db)
    row = {"id": 5, "channel": "telegram", "chat_ref": "42", "attempts": 1, "received_at": 1, "text": "fixed it",
           "payload": {"message_id": 9, "date": 1, "reply_to": {"message_id": 101, "text": "Full alert te", "from_bot": True}}}
    resolve_reply_to(store, row)
    prompt = format_turn_prompt(row)
    assert '<replying_to source="credential-check"' in prompt and "Full alert text with details" in prompt
    assert prompt.index("<replying_to") < prompt.index("<channel")


def test_reply_to_unknown_message_falls_back_to_quoted_text(tmp_path):
    store = BridgeStore(tmp_path / "i.db")
    row = {"id": 5, "channel": "telegram", "chat_ref": "42", "attempts": 1, "received_at": 1, "text": "?",
           "payload": {"reply_to": {"message_id": 7, "text": "my own earlier msg", "from_bot": False}}}
    resolve_reply_to(store, row)
    assert '<replying_to source="user">\nmy own earlier msg' in format_turn_prompt(row)


def test_bridge_flush_stores_message_ids_for_lookup(tmp_path):
    from telegram_bridge import Bridge

    class API:
        n = 500

        def call(self, method, params=None, timeout=15):
            API.n += 1
            return {"message_id": API.n}

    store = BridgeStore(tmp_path / "i.db")
    oid = store.enqueue_outbound("telegram", "42", "agent answer")
    Bridge(API(), tmp_path / "i.db", {"42"}).flush_outbox_once(store)
    assert store.find_sent("42", 501)["id"] == oid


def test_seed_shows_notes_once_and_labels_them(tmp_path):
    db = tmp_path / "i.db"
    store = BridgeStore(db)
    store.add_inbound("telegram", "42", "hello", now=1)
    store.enqueue_outbound("telegram", "42", "hi there", now=2)
    notify.send("Backup failed", source="backup", chat_id="42", token="t", db=db, sender=counting_sender()[0])
    rows = store.recent_conversation("42")
    assert [r["dir"] for r in rows] == ["in", "out", "note"]  # the sent alert row isn't duplicated
    seed = format_seed(rows)
    assert "Sent outside this chat: [backup] sent you: Backup failed" in seed
    assert "User: [backup]" not in seed


def test_folded_note_overflow_is_counted(tmp_path):
    store = BridgeStore(tmp_path / "i.db")
    for i in range(25):
        store.add_inbound("scheduler", "42", f"note {i}", kind="context_note", session_key="42")
    store.add_inbound("telegram", "42", "hi", session_key="42")
    row = store.claim_next()
    assert len(row["notes"]) == 20 and row["notes_hidden"] == 5
    assert "+5 older notes not shown" in format_turn_prompt(row)


def test_old_database_gains_new_outbox_columns(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL, chat_ref TEXT NOT NULL,"
              " text TEXT NOT NULL, files TEXT NOT NULL DEFAULT '[]', in_reply_to INTEGER, created_at REAL NOT NULL,"
              " status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL,"
              " sent_at REAL, error TEXT)")
    c.execute("INSERT INTO outbox(channel,chat_ref,text,created_at,next_attempt_at) VALUES('telegram','1','old',1,1)")
    c.commit()
    c.close()
    store = BridgeStore(db)
    row = store._conn.execute("SELECT kind, noted, source FROM outbox").fetchone()
    assert (row["kind"], row["noted"], row["source"]) == ("reply", 0, None)
