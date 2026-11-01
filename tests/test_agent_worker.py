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
    assert "via=" not in p
    row["payload"]["via"] = "web"
    assert 'via="web"' in format_turn_prompt(row)


def test_recover_in_flight_on_start(tmp_path):
    store, w, _ = make(tmp_path)
    store.add_inbound("telegram", "1", "hello", external_id="1")
    store.claim_next()  # crashed worker left it processing
    assert store.stats()["inbox_processing"] == 1
    store.recover_in_flight()
    assert asyncio.run(w.process_one()) is True


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


class CompactSession(FakeSession):
    def __init__(self):
        super().__init__()
        self.compacted = 0
        self.rotated = 0
        self.rotate_now = False

    async def compact(self):
        self.compacted += 1

    def should_rotate(self):
        return self.rotate_now

    async def rotate(self):
        self.rotated += 1
        self.rotate_now = False


def test_rotation_happens_before_turn(tmp_path):
    store, w, _ = make(tmp_path)
    s = CompactSession()
    s.rotate_now = True
    w.session_factory = lambda key: s
    store.add_inbound("telegram", "1", "hi", external_id="1")
    asyncio.run(w.process_one())
    assert s.rotated == 1 and s.prompts


def test_maintenance_compacts_only_in_quiet_hour_when_idle_and_big(tmp_path):
    from datetime import datetime

    store, w, _ = make(tmp_path)
    s = CompactSession()
    w.sessions["1"] = s
    store.kv_set("ctx_tokens:1", str(agent_worker.COMPACT_MIN_TOKENS + 1))
    assert asyncio.run(w.maintenance_once(datetime(2026, 10, 7, 14, 0))) is False  # not 3am
    store.add_inbound("telegram", "1", "busy", external_id="1")
    assert asyncio.run(w.maintenance_once(datetime(2026, 10, 7, 3, 0))) is False  # not idle
    store._conn.execute("UPDATE inbox SET status='done'")
    assert asyncio.run(w.maintenance_once(datetime(2026, 10, 7, 3, 5))) is True
    assert s.compacted == 1
    assert asyncio.run(w.maintenance_once(datetime(2026, 10, 7, 3, 10))) is False  # once per day


def test_small_context_is_not_compacted(tmp_path):
    from datetime import datetime

    store, w, _ = make(tmp_path)
    s = CompactSession()
    w.sessions["1"] = s
    store.kv_set("ctx_tokens:1", "100")
    asyncio.run(w.maintenance_once(datetime(2026, 10, 7, 3, 0)))
    assert s.compacted == 0


def test_daily_cost_alert_once(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_DAILY_COST_ALERT_USD", "1.00")
    monkeypatch.setenv("BRIDGE_ALLOWED_CHAT_IDS", "1")
    store, w, _ = make(tmp_path)
    w._track_daily_cost(0.6)
    assert _alerts(store) == []
    w._track_daily_cost(0.6)
    w._track_daily_cost(5)
    alerts = _alerts(store)
    assert len(alerts) == 1 and "$" in alerts[0]


def test_seed_format_and_fresh_start(tmp_path):
    store = BridgeStore(tmp_path / "i.db")
    store.add_inbound("telegram", "1", "remember PELICAN", external_id="1", now=time.time() - 60)
    store.enqueue_outbound("telegram", "1", "stored", now=time.time() - 50)
    s = agent_worker.SdkSession("1", store, "sonnet")
    store.kv_set("session_id:1", "dead-session")
    s._start_fresh()
    assert store.kv_get("session_id:1") == ""
    assert "remember PELICAN" in s.seed and "Assistant: stored" in s.seed


def test_compact_accepts_result_without_assistant_message(tmp_path):
    from claude_agent_sdk import ResultMessage

    class FakeClient:
        async def query(self, prompt):
            pass

        async def receive_messages(self):
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=0, is_error=False, num_turns=0, session_id="new-sid", result=None, total_cost_usd=0.1, usage={"input_tokens": 0})

    store = BridgeStore(tmp_path / "i.db")
    s = agent_worker.SdkSession("1", store, "sonnet")
    s._client = FakeClient()

    async def ready():
        pass

    s.ensure_ready = ready
    asyncio.run(asyncio.wait_for(s.compact(), 5))  # would hang forever before the fix
    assert store.kv_get("session_id:1") == "new-sid"


def test_brand_new_session_is_seeded_from_message_log(tmp_path):
    import types, sys as _sys

    store = BridgeStore(tmp_path / "i.db")
    store.add_inbound("telegram", "1", "earlier question", external_id="1", now=time.time() - 300)
    store.enqueue_outbound("telegram", "1", "earlier answer", now=time.time() - 290)
    s = agent_worker.SdkSession("1", store, "sonnet")

    class FakeClient:
        def __init__(self, options=None):
            pass

        async def connect(self):
            pass

        async def get_mcp_status(self):
            return {"mcpServers": []}

    import claude_agent_sdk
    orig = claude_agent_sdk.ClaudeSDKClient
    claude_agent_sdk.ClaudeSDKClient = FakeClient
    try:
        asyncio.run(s.ensure_ready())
    finally:
        claude_agent_sdk.ClaudeSDKClient = orig
    assert "earlier question" in s.seed and "earlier answer" in s.seed


# ---------------------------------------------------------------- concurrent sessions

class GateSession(FakeSession):
    """Blocks inside run_turn until its gate is opened; records start/finish order."""

    def __init__(self, key, log, gates, fail=False):
        super().__init__()
        self.key, self.log, self.gates, self.fail = key, log, gates, fail

    async def run_turn(self, prompt, on_event):
        self.prompts.append(prompt)
        self.log.append(("start", self.key, prompt))
        gate = self.gates.get(self.key)
        if gate is not None:
            await gate.wait()
        if self.fail:
            raise RuntimeError("sdk died")
        self.log.append(("end", self.key, prompt))
        return TurnResult(text=f"done {self.key}", session_id="s", cost_usd=0.0, usage={})


def make_concurrent(tmp_path, gates=None, fail_keys=(), **kw):
    agent_worker.STREAM_LOG = tmp_path / "stream.jsonl"
    agent_worker.USAGE_LOG = tmp_path / "usage.jsonl"
    store = BridgeStore(tmp_path / "i.db")
    log, gates = [], gates if gates is not None else {}
    w = Worker(store, lambda key: GateSession(key, log, gates, fail=key in fail_keys), **kw)
    return store, w, log, gates


async def _until(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


def _replies(store, chat):
    return [r["text"] for r in store._conn.execute("SELECT text FROM outbox WHERE chat_ref=? AND kind='reply'", (chat,))]


def test_blocked_session_does_not_block_another(tmp_path):
    async def go():
        store, w, log, gates = make_concurrent(tmp_path, gates={"A": asyncio.Event()})
        store.add_inbound("telegram", "A", "long dev task", external_id="1")
        store.add_inbound("telegram", "B", "turn off the lights", external_id="2")
        run = asyncio.create_task(w.run())
        assert await _until(lambda: _replies(store, "B") == ["done B"])
        assert _replies(store, "A") == []  # A is still mid-turn
        assert set(store.current_turns()) == {"A"}
        gates["A"].set()
        assert await _until(lambda: _replies(store, "A") == ["done A"])
        w.stop.set()
        await asyncio.wait_for(run, 5)
        assert store.stats()["inbox_pending"] == 0 and store.stats()["inbox_processing"] == 0
        assert store.current_turns() == {}

    asyncio.run(go())


def test_same_session_order_preserved(tmp_path):
    async def go():
        store, w, log, gates = make_concurrent(tmp_path, gates={"A": asyncio.Event()})
        store.add_inbound("telegram", "A", "first", external_id="1")
        store.add_inbound("telegram", "A", "second", external_id="2")
        run = asyncio.create_task(w.run())
        assert await _until(lambda: any(e[0] == "start" for e in log))
        await asyncio.sleep(0.5)
        assert [e[:2] for e in log] == [("start", "A")]  # second must wait for the first
        gates["A"].set()
        assert await _until(lambda: len(_replies(store, "A")) == 2)
        w.stop.set()
        await asyncio.wait_for(run, 5)
        order = [("first" in e[2], e[0]) for e in log]
        assert order == [(True, "start"), (True, "end"), (False, "start"), (False, "end")]

    asyncio.run(go())


def test_paused_stops_all_sessions_then_resumes(tmp_path):
    async def go():
        store, w, log, gates = make_concurrent(tmp_path)
        store.kv_set("paused", "1")
        store.add_inbound("telegram", "A", "a", external_id="1")
        store.add_inbound("telegram", "B", "b", external_id="2")
        run = asyncio.create_task(w.run())
        await asyncio.sleep(0.8)
        assert log == [] and store.stats()["inbox_pending"] == 2
        store.kv_set("paused", "0")
        assert await _until(lambda: store.stats()["inbox_pending"] == 0 and len(log) == 4)
        w.stop.set()
        await asyncio.wait_for(run, 5)

    asyncio.run(go())


def test_failure_in_one_session_does_not_kill_the_other(tmp_path):
    async def go():
        store, w, log, gates = make_concurrent(tmp_path, fail_keys=("A",))
        store.add_inbound("telegram", "A", "boom", external_id="1")
        run = asyncio.create_task(w.run())
        assert await _until(lambda: any(e[:2] == ("start", "A") for e in log))
        store.add_inbound("telegram", "B", "fine", external_id="2")
        assert await _until(lambda: _replies(store, "B") == ["done B"])
        w.stop.set()
        await asyncio.wait_for(run, 5)

    asyncio.run(go())


def test_loop_exception_outside_turn_is_contained_and_respawned(tmp_path):
    async def go():
        store, w, log, gates = make_concurrent(tmp_path)
        real = w.process_one
        calls = {"n": 0}

        async def flaky(key=None):
            if key == "A" and calls["n"] == 0:
                calls["n"] += 1
                raise RuntimeError("db hiccup")
            return await real(key)

        w.process_one = flaky
        store.add_inbound("telegram", "A", "a", external_id="1")
        store.add_inbound("telegram", "B", "b", external_id="2")
        run = asyncio.create_task(w.run())
        assert await _until(lambda: _replies(store, "B") == ["done B"])
        assert await _until(lambda: _replies(store, "A") == ["done A"], timeout=6)  # respawned after backoff
        w.stop.set()
        await asyncio.wait_for(run, 5)

    asyncio.run(go())


def test_idle_session_loops_exit_and_are_not_leaked(tmp_path):
    async def go():
        store, w, log, gates = make_concurrent(tmp_path)
        store.add_inbound("telegram", "A", "a", external_id="1")
        run = asyncio.create_task(w.run())
        assert await _until(lambda: _replies(store, "A") == ["done A"])
        assert await _until(lambda: w._loops == {})
        w.stop.set()
        await asyncio.wait_for(run, 5)

    asyncio.run(go())


def test_concurrent_sessions_are_capped(tmp_path):
    async def go():
        gates = {k: asyncio.Event() for k in "ABC"}
        store, w, log, _ = make_concurrent(tmp_path, gates=gates, max_sessions=2)
        for i, k in enumerate("ABC"):
            store.add_inbound("telegram", k, k, external_id=str(i))
        run = asyncio.create_task(w.run())
        assert await _until(lambda: len(store.current_turns()) == 2)
        await asyncio.sleep(0.5)
        assert set(store.current_turns()) == {"A", "B"}  # C waits for a free slot
        gates["A"].set()
        assert await _until(lambda: "C" in {e[1] for e in log})
        for g in gates.values():
            g.set()
        assert await _until(lambda: store.stats()["inbox_pending"] == 0 and store.stats()["inbox_processing"] == 0)
        w.stop.set()
        await asyncio.wait_for(run, 5)

    asyncio.run(go())


def test_stop_cancels_unfinished_turn_and_redelivers(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_worker, "STOP_GRACE_S", 0.3)

    async def go():
        store, w, log, gates = make_concurrent(tmp_path, gates={"A": asyncio.Event()})
        store.add_inbound("telegram", "A", "never finishes", external_id="1")
        store.add_inbound("telegram", "B", "quick", external_id="2")
        run = asyncio.create_task(w.run())
        assert await _until(lambda: _replies(store, "B") == ["done B"])
        w.stop.set()
        await asyncio.wait_for(run, 5)
        # A's message is back to pending for the next start; nothing is left marked as running.
        assert store.stats()["inbox_pending"] == 1 and store.stats()["inbox_processing"] == 0
        assert store.current_turns() == {}

    asyncio.run(go())


def test_current_turn_is_a_per_session_map(tmp_path):
    async def go():
        store, w, log, gates = make_concurrent(tmp_path, gates={"A": asyncio.Event(), "B": asyncio.Event()})
        store.add_inbound("telegram", "A", "a", external_id="1")
        store.add_inbound("telegram", "B", "b", external_id="2")
        run = asyncio.create_task(w.run())
        assert await _until(lambda: len(store.current_turns()) == 2)
        snap = store.health_snapshot()
        assert {t["session"] for t in snap["current_turns"]} == {"A", "B"}
        assert snap["current_turn"]["session"] in ("A", "B")
        gates["A"].set()
        assert await _until(lambda: set(store.current_turns()) == {"B"})  # A's marker cleared, B's untouched
        gates["B"].set()
        assert await _until(lambda: store.current_turns() == {})
        w.stop.set()
        await asyncio.wait_for(run, 5)

    asyncio.run(go())


def test_old_single_turn_value_is_tolerated_and_cleared_on_start(tmp_path):
    store = BridgeStore(tmp_path / "i.db")
    store.kv_set("current_turn", '{"inbox_id": 5, "started": %f}' % (time.time() - 10))
    snap = store.health_snapshot()
    assert snap["current_turns"][0]["session"] == "legacy" and snap["current_turns"][0]["inbox_id"] == 5
    store.kv_set("current_turn", "not json")
    assert store.health_snapshot()["current_turns"] == []
    store.kv_set("current_turn", '{"inbox_id": 5, "started": 1}')
    store.kv_set("current_turn:A", '{"inbox_id": 6, "started": 1}')

    async def go():
        w = Worker(store, lambda key: FakeSession())
        run = asyncio.create_task(w.run())
        await asyncio.sleep(0.2)
        assert store.current_turns() == {}  # stale markers from a killed worker are dropped
        w.stop.set()
        await asyncio.wait_for(run, 5)

    asyncio.run(go())


def test_mcp_down_is_the_union_across_sessions(tmp_path):
    store = BridgeStore(tmp_path / "i.db")
    store.set_mcp_down("A", ["todoist"])
    store.set_mcp_down("B", [])  # B's view must not wipe A's
    assert store.health_snapshot()["mcp_down"] == ["todoist"]
    store.set_mcp_down("B", ["ha"])
    assert store.health_snapshot()["mcp_down"] == ["ha", "todoist"]
    store.set_mcp_down("A", [])
    assert store.health_snapshot()["mcp_down"] == ["ha"]


def test_ping_skips_only_the_busy_session(tmp_path):
    store, w, _ = make(tmp_path)
    store.set_current_turn("A", 1, time.time())
    busy, idle = PingSession([RuntimeError("would fail")]), PingSession([[]])
    w.sessions["A"], w.sessions["B"] = busy, idle
    asyncio.run(w.ping_once())
    assert busy.results and not idle.results  # A untouched, B pinged
    assert store.health_snapshot()["last_canary"]["ok"] is True


def test_model_canary_runs_while_a_session_is_busy(tmp_path):
    store, w, _ = make(tmp_path)
    store.set_current_turn("A", 1, time.time())
    ran = []

    async def ok():
        ran.append(1)

    asyncio.run(w.model_canary_once(ok))
    assert ran and store.health_snapshot()["last_canary"]["kind"] == "model"


def test_maintenance_compacts_idle_session_while_another_is_busy(tmp_path):
    from datetime import datetime

    store, w, _ = make(tmp_path)
    a, b = CompactSession(), CompactSession()
    w.sessions["A"], w.sessions["B"] = a, b
    for k in "AB":
        store.kv_set(f"ctx_tokens:{k}", str(agent_worker.COMPACT_MIN_TOKENS + 1))
    store.add_inbound("telegram", "A", "busy", external_id="1")
    store.claim_next("A")
    store.set_current_turn("A", 1, time.time())
    assert asyncio.run(w.maintenance_once(datetime(2026, 10, 7, 3, 0))) is False  # A deferred, day not done
    assert (a.compacted, b.compacted) == (0, 1)
    store.complete(1)
    store.clear_current_turn("A")
    assert asyncio.run(w.maintenance_once(datetime(2026, 10, 7, 3, 1))) is True
    assert (a.compacted, b.compacted) == (1, 1)  # B is not compacted twice
    assert store.current_turns() == {}


def test_sdk_options_dev_note_only_for_dev_chats(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_DEV_CHAT_IDS", "-100")
    monkeypatch.setenv("BRIDGE_SESSION_MAP", "{}")
    store = BridgeStore(tmp_path / "i.db")
    dev = agent_worker.SdkSession("-100", store, "sonnet")._options().system_prompt["append"]
    home = agent_worker.SdkSession("1", store, "sonnet")._options().system_prompt["append"]
    assert "git worktree add" in dev and "never switch its branch" in dev
    assert dev.startswith(agent_worker.CHANNEL_PROMPT)  # stable shared prefix, note appended after
    assert home == agent_worker.CHANNEL_PROMPT and "worktree" not in home


def test_no_dev_session_when_unconfigured(monkeypatch):
    import paths
    monkeypatch.delenv("BRIDGE_DEV_CHAT_IDS", raising=False)
    monkeypatch.setattr(paths, "get_env", lambda k, *a, **kw: None)
    assert agent_worker.is_dev_session("-100") is False


def test_alert_never_lands_in_dev_chat(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_DEV_CHAT_IDS", "-100")
    monkeypatch.setenv("BRIDGE_ALLOWED_CHAT_IDS", "-100,1")
    monkeypatch.delenv("TELEGRAM_ALERT_CHAT_ID", raising=False)
    store, w, _ = make(tmp_path)
    w._alert("hello")
    assert [r["chat_ref"] for r in store._conn.execute("SELECT chat_ref FROM outbox")] == ["1"]
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "-100")  # misconfigured: still protected
    w._alert("again")
    assert {r["chat_ref"] for r in store._conn.execute("SELECT chat_ref FROM outbox")} == {"1"}


def test_context_notes_follow_session_key(tmp_path):
    store = BridgeStore(tmp_path / "i.db")
    store.add_inbound("scheduled:x", "1", "household note", kind="context_note", session_key="1")
    store.add_inbound("telegram", "-100", "dev question", external_id="1", session_key="-100")
    row = store.claim_next("-100")
    assert row["notes"] == []  # household notes stay out of the dev session


# ---------------------------------------------------------------- review fixes

def test_ping_alerts_are_one_per_outage_across_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_ALLOWED_CHAT_IDS", "1")
    monkeypatch.delenv("TELEGRAM_ALERT_CHAT_ID", raising=False)
    store, w, _ = make(tmp_path)
    hung = PingSession([RuntimeError("hung")] * 2)
    healthy = PingSession([[], [], []])
    w.sessions["A"], w.sessions["B"] = hung, healthy
    asyncio.run(w.ping_once())  # A fails, B ok: one failure alert, no recovery flip
    asyncio.run(w.ping_once())  # A fails again and is rebuilt: still the same outage
    alerts = _alerts(store)
    assert len(alerts) == 1 and "failed (ping)" in alerts[0]
    asyncio.run(w.ping_once())  # only B left, all ok: recovered once
    alerts = _alerts(store)
    assert len(alerts) == 1 and "recovered" in alerts[0]


def test_ping_skips_session_whose_lock_is_held(tmp_path):
    async def go():
        store, w, _ = make(tmp_path)
        s = PingSession([RuntimeError("would fail")])
        w.sessions["A"] = s
        async with w._lock("A"):  # e.g. compaction in progress, no turn marker
            await w.ping_once()
        assert s.results and store.kv_get("last_canary") is None

    asyncio.run(go())


def test_turn_queued_behind_compaction_keeps_marker_and_slow_acks(tmp_path):
    async def go():
        store, w, log, gates = make_concurrent(tmp_path, slow_ack_after=0.1)
        lock = w._lock("A")
        await lock.acquire()  # maintenance holds the session
        store.set_current_turn("A", "maintenance", time.time())
        store.add_inbound("telegram", "A", "hi", external_id="1")
        task = asyncio.create_task(w.process_one("A"))
        await asyncio.sleep(0.4)
        assert store.current_turns()["A"]["inbox_id"] == "maintenance"  # the waiting turn did not overwrite it
        assert any("On it" in r["text"] for r in store._conn.execute("SELECT text FROM outbox"))
        store.clear_current_turn("A")  # compaction finishes
        lock.release()
        assert await asyncio.wait_for(task, 5) is True
        assert _replies(store, "A") == ["done A"] and store.current_turns() == {}

    asyncio.run(go())


def test_exception_after_claim_requeues_instead_of_stranding(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_worker, "RETRY_BACKOFF_S", 0)
    store, w, log, gates = make_concurrent(tmp_path)
    store.add_inbound("telegram", "A", "hi", external_id="1")
    real = store.set_current_turn
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return real(*a, **k)

    store.set_current_turn = boom
    assert asyncio.run(w.process_one("A")) is True  # swallowed, not raised
    assert store.stats()["inbox_pending"] == 1 and store.stats()["inbox_processing"] == 0
    assert asyncio.run(w.process_one("A")) is True  # retried and completed
    assert _replies(store, "A") == ["done A"]


def test_repeated_unexpected_failure_hits_max_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_worker, "RETRY_BACKOFF_S", 0)
    store, w, log, gates = make_concurrent(tmp_path)
    store.add_inbound("telegram", "A", "hi", external_id="1")
    store.set_current_turn = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("always"))
    for _ in range(agent_worker.MAX_ATTEMPTS + 1):
        asyncio.run(w.process_one("A"))
    assert store.stats()["inbox_pending"] == 0 and store.stats()["inbox_processing"] == 0
    assert "couldn't process" in store.next_outbound("telegram")["text"]


def test_unexpected_failure_backs_off(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_worker, "RETRY_BACKOFF_S", 0.3)
    store, w, log, gates = make_concurrent(tmp_path)
    store.add_inbound("telegram", "A", "hi", external_id="1")
    store.set_current_turn = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("locked"))
    t0 = time.time()
    asyncio.run(w.process_one("A"))
    assert time.time() - t0 >= 0.3


def test_closed_sessions_leave_no_mcp_state(tmp_path):
    store = BridgeStore(tmp_path / "i.db")
    store.set_mcp_down("A", ["ha"])
    store.clear_mcp_down("A")
    assert store.health_snapshot()["mcp_down"] == [] and store.kv_get("mcp_down:A") is None
    store.set_mcp_down("B", ["x"])
    store.clear_mcp_down()
    assert store.health_snapshot()["mcp_down"] == []

    async def go():
        w = Worker(store, lambda key: FakeSession())
        w.sessions["Z"] = FakeSession()
        store.set_mcp_down("Z", ["stale"])
        run = asyncio.create_task(w.run())
        await asyncio.sleep(0.2)
        assert store.health_snapshot()["mcp_down"] == []  # start clears the previous run's keys
        store.set_mcp_down("Z", ["stale"])
        w.stop.set()
        await asyncio.wait_for(run, 5)
        assert store.health_snapshot()["mcp_down"] == [] and w.sessions["Z"].closed

    asyncio.run(go())


def test_dev_prompt_mentions_worktree_first_and_no_live_state():
    p = agent_worker.DEV_PROMPT
    assert "worktree FIRST" in p and "state/" in p and "live database" in p and "dirty" in p
