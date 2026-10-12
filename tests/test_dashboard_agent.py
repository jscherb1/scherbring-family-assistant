import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from fastapi.testclient import TestClient  # noqa: E402

from bridge_store import BridgeStore  # noqa: E402
from dashboard.app import app  # noqa: E402
from dashboard.data import agent  # noqa: E402


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("DASHBOARD_SHOW_AGENT_CONTENT", raising=False)
    (tmp_path / "logs").mkdir()
    store = BridgeStore(tmp_path / "inbox.db")
    now = time.time()
    store.kv_set("bridge_heartbeat", str(now))
    store.kv_set("last_poll_ok", str(now))
    store.kv_set("worker_heartbeat", str(now))
    store.kv_set("last_canary", json.dumps({"t": now, "kind": "ping", "ok": True, "detail": ""}))
    store.add_inbound("telegram", "1", "hi", external_id="1")
    secret = "SECRET-FINANCE-NUMBER-4242"
    events = [
        {"t": "2026-10-06T10:00:00", "inbox_id": 1, "type": "turn_start", "text": secret},
        {"t": "2026-10-06T10:00:01", "inbox_id": 1, "type": "tool_use", "name": "Bash", "input": {"command": secret}},
        {"t": "2026-10-06T10:00:02", "inbox_id": 1, "type": "tool_result", "is_error": False, "content": secret},
        {"t": "2026-10-06T10:00:05", "inbox_id": 1, "type": "turn_end", "duration_s": 5.0},
    ]
    (tmp_path / "logs" / "agent_stream.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    usage = {"t": "2026-10-06T10:00:05", "cost_usd": 0.1, "usage": {"input_tokens": 2, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 50, "output_tokens": 7}}
    (tmp_path / "usage.jsonl").write_text(json.dumps(usage) + "\n")
    return tmp_path, secret


def test_status_reports_liveness_and_queue(state):
    s = agent.get_agent_status()
    assert s["bridge_up"] and s["worker_up"] and s["polling_ok"]
    assert s["inbox_pending"] == 1 and s["last_canary"]["ok"] is True


def test_dashboard_never_writes_to_inbox(state):
    tmp, _ = state
    before = (tmp / "inbox.db").stat().st_mtime_ns
    agent.get_agent_status()
    assert (tmp / "inbox.db").stat().st_mtime_ns == before


def test_status_error_when_no_inbox(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_STATE_DIR", str(tmp_path))
    assert "error" in agent.get_agent_status()


def test_stream_hides_content_by_default(state):
    _, secret = state
    client = TestClient(app)
    for url in ("/agent", "/agent/panel", "/api/agent"):
        body = client.get(url).text
        assert secret not in body, url
    assert "Bash" in client.get("/agent").text  # tool names are metadata, fine to show


def test_stream_shows_content_when_opted_in(state, monkeypatch):
    _, secret = state
    monkeypatch.setenv("DASHBOARD_SHOW_AGENT_CONTENT", "1")
    assert secret in TestClient(app).get("/agent/panel").text


def test_usage_page_and_context_size(state):
    u = agent.get_usage()
    assert u["last_context_tokens"] == 152
    assert u["days"][0]["turns"] == 1 and u["days"][0]["cache_read"] == 100
    r = TestClient(app).get("/agent/usage")
    assert r.status_code == 200 and "152" in r.text
