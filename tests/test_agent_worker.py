import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import agent_worker  # noqa: E402
from agent_worker import TurnResult, Worker, format_turn_prompt  # noqa: E402
from bridge_store import BridgeStore  # noqa: E402


class FakeSession:
    def __init__(self, behavior="ok"):
        self.behavior = behavior
        self.prompts = []
        self.closed = False

    async def ensure_ready(self):
        pass

    async def run_turn(self, prompt, on_event):
        self.prompts.append(prompt)
        on_event({"type": "text", "text": "thinking"})
        if self.behavior == "slow":
            await asyncio.sleep(5)
        if self.behavior == "crash":
            raise RuntimeError("sdk died")
        return TurnResult(text="the answer", session_id="s1", cost_usd=0.01, usage={"input_tokens": 1})

    async def close(self):
        self.closed = True


def make(tmp_path, behavior="ok", **kw):
    agent_worker.STREAM_LOG = tmp_path / "stream.jsonl"
    agent_worker.USAGE_LOG = tmp_path / "usage.jsonl"
    store = BridgeStore(tmp_path / "i.db")
    sessions = {}

    def factory(key):
        sessions[key] = FakeSession(behavior)
        return sessions[key]

    return store, Worker(store, factory, **kw), sessions


def test_turn_replies_and_completes(tmp_path):
    store, w, sessions = make(tmp_path)
    store.add_inbound("telegram", "1", "hello", external_id="1", payload={"message_id": 7, "user": "j"})
    assert asyncio.run(w.process_one()) is True
    out = store.next_outbound("telegram")
    assert out["text"] == "the answer" and out["chat_ref"] == "1"
    assert store.stats()["inbox_pending"] == 0 and store.stats()["inbox_processing"] == 0
    assert '<channel source="telegram" chat_id="1"' in sessions["1"].prompts[0]
    assert (tmp_path / "usage.jsonl").exists() and (tmp_path / "stream.jsonl").exists()


def test_idle_returns_false(tmp_path):
    _, w, _ = make(tmp_path)
    assert asyncio.run(w.process_one()) is False


def test_crash_requeues_then_gives_up(tmp_path):
    store, w, sessions = make(tmp_path, behavior="crash")
    store.add_inbound("telegram", "1", "hello", external_id="1")
    for _ in range(agent_worker.MAX_ATTEMPTS):
        asyncio.run(w.process_one())
    assert store.stats()["inbox_pending"] == 1  # still waiting for the 4th (give-up) pass
    asyncio.run(w.process_one())
    assert store.stats()["inbox_pending"] == 0
    assert "couldn't process" in store.next_outbound("telegram")["text"]


def test_timeout_notifies_and_completes(tmp_path):
    store, w, _ = make(tmp_path, behavior="slow", turn_timeout=0.2, slow_ack_after=999)
    store.add_inbound("telegram", "1", "hello", external_id="1")
    asyncio.run(w.process_one())
    assert "too long" in store.next_outbound("telegram")["text"]
    assert store.stats()["inbox_pending"] == 0


def test_slow_turn_sends_single_ack(tmp_path):
    store, w, _ = make(tmp_path, behavior="slow", turn_timeout=3, slow_ack_after=0.1)
    store.add_inbound("telegram", "1", "hello", external_id="1")
    asyncio.run(w.process_one())
    texts = []
    while (o := store.next_outbound("telegram")) is not None:
        texts.append(o["text"])
        store.mark_sent(o["id"])
    assert sum("On it" in t for t in texts) == 1


def test_prompt_includes_notes_and_retry_marker():
    row = {
        "id": 1, "channel": "telegram", "chat_ref": "1", "text": "what did that mean?", "attempts": 2,
        "received_at": time.time(), "payload": {"message_id": 3, "user": "j"},
        "notes": [{"text": "weekly summary delivered", "received_at": time.time()}],
    }
    p = format_turn_prompt(row)
    assert "[Earlier notes]" in p and "weekly summary delivered" in p
    assert "Retry #2" in p and "what did that mean?" in p


def test_recover_in_flight_on_start(tmp_path):
    store, w, _ = make(tmp_path)
    store.add_inbound("telegram", "1", "hello", external_id="1")
    store.claim_next()  # crashed worker left it processing
    assert store.stats()["inbox_processing"] == 1
    store.recover_in_flight()
    assert asyncio.run(w.process_one()) is True
