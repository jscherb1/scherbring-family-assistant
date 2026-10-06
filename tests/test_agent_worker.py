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


def test_empty_result_still_replies(tmp_path):
    store, w, sessions = make(tmp_path)

    class Empty(FakeSession):
        async def run_turn(self, prompt, on_event):
            return TurnResult(text="")

    w.session_factory = lambda key: Empty()
    store.add_inbound("telegram", "1", "hello", external_id="1")
    asyncio.run(w.process_one())
    assert "Done" in store.next_outbound("telegram")["text"]


def test_stale_result_before_assistant_output_is_ignored(tmp_path):
    """SdkSession must skip a leading ResultMessage (e.g. finished background task)."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    def result(text):
        return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s", result=text, total_cost_usd=0.0, usage={})

    class FakeClient:
        async def query(self, prompt):
            pass

        async def receive_messages(self):
            yield result("stale background output")
            yield AssistantMessage(content=[TextBlock(text="real answer")], model="m")
            yield result("real answer")

    store = BridgeStore(tmp_path / "i.db")
    s = agent_worker.SdkSession("1", store, "sonnet")
    s._client = FakeClient()

    async def ready():
        pass

    s.ensure_ready = ready
    events = []
    res = asyncio.run(s.run_turn("hi", events.append))
    assert res.text == "real answer"
    assert any(e["type"] == "stale_result_ignored" for e in events)


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


def test_context_bridge_hooks_noop_for_bridge_worker(monkeypatch, capsys):
    import telegram_context_bridge as tcb

    monkeypatch.setenv("BRIDGE_WORKER", "1")
    assert tcb.main(["hook-session-start"]) == 0
    assert capsys.readouterr().out == ""


class PingSession(FakeSession):
    def __init__(self, results):
        super().__init__()
        self.results = list(results)

    async def ping(self):
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _alerts(store):
    out = []
    while (o := store.next_outbound("telegram")) is not None:
        out.append(o["text"])
        store.mark_sent(o["id"])
    return out


def test_ping_failure_alerts_once_then_recovers(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_ALLOWED_CHAT_IDS", "1")
    store, w, _ = make(tmp_path)
    s = PingSession([RuntimeError("x"), RuntimeError("y"), []])
    w.sessions["1"] = s
    asyncio.run(w.ping_once())  # fail 1: alert
    asyncio.run(w.ping_once())  # fail 2: rebuild client, no second alert
    assert s.closed and "1" not in w.sessions
    alerts = _alerts(store)
    assert len(alerts) == 1 and "failed (ping)" in alerts[0]
    w.sessions["1"] = PingSession([[]])
    asyncio.run(w.ping_once())
    assert any("recovered" in a for a in _alerts(store))
    assert store.health_snapshot()["last_canary"]["ok"] is True


def test_ping_skipped_while_turn_running(tmp_path):
    store, w, _ = make(tmp_path)
    store.kv_set("current_turn", '{"inbox_id": 1}')
    w.sessions["1"] = PingSession([RuntimeError("would fail")])
    asyncio.run(w.ping_once())
    assert store.kv_get("last_canary") is None


def test_model_canary_records_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_ALLOWED_CHAT_IDS", "1")
    store, w, _ = make(tmp_path)

    async def boom():
        raise RuntimeError("auth expired")

    asyncio.run(w.model_canary_once(boom))
    assert store.health_snapshot()["last_canary"]["ok"] is False
    assert "failed (model)" in _alerts(store)[0]
