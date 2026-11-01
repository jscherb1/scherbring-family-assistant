"""Telegram bridge / agent worker liveness, live event stream and usage (read-only)."""
import json
import os
import sys
from collections import defaultdict

from . import common

sys.path.insert(0, str(common.SCRIPTS_DIR))
import fastlane  # noqa: E402
from bridge_store import BridgeStore  # noqa: E402
from paths import dev_chat_ids  # noqa: E402

STREAM_TAIL = 80
USAGE_DAYS = 14


def _show_content() -> bool:
    # The dashboard has no login, and the agent stream carries message text, tool inputs and results
    # (finance, kids' memories...). Default to metadata only; opt in when bound to localhost.
    return os.environ.get("DASHBOARD_SHOW_AGENT_CONTENT") == "1"


@common.guarded
def get_agent_status():
    path = common.state_dir() / "inbox.db"
    if not path.exists():
        raise FileNotFoundError("inbox.db not found")
    snap = BridgeStore(path, read_only=True).health_snapshot()
    snap["content_visible"] = _show_content()
    dev = dev_chat_ids()
    # A label instead of the chat id: this page and /api/agent have no login.
    label = lambda k: "dev chat" if k in dev else "household"  # noqa: E731
    for t in snap["current_turns"]:
        t["label"] = label(t.pop("session"))
    if snap.get("current_turn"):
        snap["current_turn"]["label"] = label(snap["current_turn"].pop("session"))
    return snap


def get_stream(limit: int = STREAM_TAIL):
    path = common.state_dir() / "logs" / "agent_stream.jsonl"
    show = _show_content()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return {"events": [], "content_visible": show}
    events = []
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        out = {"t": ev.get("t", ""), "inbox_id": ev.get("inbox_id"), "type": ev.get("type"), "name": ev.get("name")}
        if ev.get("duration_s") is not None:
            out["duration_s"] = ev["duration_s"]
        if ev.get("is_error"):
            out["is_error"] = True
        if show:
            if ev.get("type") == "tool_use":
                out["detail"] = json.dumps(ev.get("input"))[:300]
            else:
                out["detail"] = str(ev.get("text") or ev.get("content") or ev.get("error") or "")[:400]
        events.append(out)
    return {"events": events[::-1], "content_visible": show}


def get_fastlane(days: int = 7):
    """Fast lane switch, task liveness, and counts / median latency per tier. No message text, no chat ids."""
    out = fastlane.usage_summary(common.state_dir() / "usage.jsonl", days)
    out["enabled"], out["alive"] = None, False
    path = common.state_dir() / "inbox.db"
    if path.exists():
        try:
            store = BridgeStore(path, read_only=True)
            out["enabled"] = store.kv_get("fastlane") == "on"
            out["alive"] = store.fastlane_gate_active()
        except Exception:  # noqa: BLE001 - the page still shows the numbers
            pass
    return out


def get_usage():
    path = common.state_dir() / "usage.jsonl"
    days = defaultdict(lambda: {"turns": 0, "cost_usd": 0.0, "output_tokens": 0, "uncached_input": 0,
                                "cache_read": 0, "cache_write": 0, "max_context": 0})
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {"days": [], "last_context_tokens": None}
    last_ctx = None
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        fl = r.get("fastlane")
        if isinstance(fl, dict) and fl.get("tier") in (0, 1):
            continue  # answered by the fast lane: no agent turn, counted under get_fastlane
        u = r.get("usage") or {}
        d = days[(r.get("t") or "")[:10]]
        d["turns"] += 1
        d["cost_usd"] += r.get("cost_usd") or 0
        d["output_tokens"] += u.get("output_tokens") or 0
        d["uncached_input"] += u.get("input_tokens") or 0
        d["cache_read"] += u.get("cache_read_input_tokens") or 0
        d["cache_write"] += u.get("cache_creation_input_tokens") or 0
        ctx = ((u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
               + (u.get("cache_creation_input_tokens") or 0))
        d["max_context"] = max(d["max_context"], ctx)
        last_ctx = ctx
    rows = []
    for k, v in sorted(days.items(), reverse=True)[:USAGE_DAYS]:
        rows.append({"date": k, **{kk: (round(vv, 3) if kk == "cost_usd" else vv) for kk, vv in v.items()}})
    return {"days": rows, "last_context_tokens": last_ctx}
