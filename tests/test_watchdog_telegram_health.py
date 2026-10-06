import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import watchdog_telegram_health as w  # noqa: E402

NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)
BAD = "Tool mcp__plugin_telegram_telegram__reply not found in render-time tools"
GOOD = 'MCP server "plugin:telegram:telegram": Successfully connected'


def ts(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def test_log_grep_unhealthy_only_when_after_last_healthy():
    assert w.evaluate([f"{ts(5)} {GOOD}", f"{ts(2)} {BAD}"], {}, None, NOW)[0]
    assert not w.evaluate([f"{ts(5)} {BAD}", f"{ts(2)} {GOOD}"], {}, None, NOW)[0]
    assert not w.evaluate(["unrelated line"], {}, None, NOW)[0]


def test_fallback_signal_respects_last_restart():
    fb = {"last_used_at": "2026-10-05T11:00:00", "reason": "tool missing"}
    assert w.evaluate(["x"], {}, fb, NOW)[0]
    later_restart = {"last_restart_at": "2026-10-05T11:30:00"}
    assert not w.evaluate(["x"], later_restart, fb, NOW)[0]


def test_stale_silent_needs_reconnect_and_60_minutes():
    reconnect = "SSETransport: Liveness timeout, reconnecting"
    lines = [f"{ts(90)} {GOOD}", f"{ts(80)} {reconnect}"]
    assert w.evaluate(lines, {}, None, NOW)[0]
    assert not w.evaluate([f"{ts(90)} {GOOD}"], {}, None, NOW)[0]  # no reconnect
    assert not w.evaluate([f"{ts(30)} {GOOD}", f"{ts(20)} {reconnect}"], {}, None, NOW)[0]  # recent


def make_repo(tmp_path, lines):
    log = tmp_path / "state" / "logs" / "orchestrator_debug.log"
    log.parent.mkdir(parents=True)
    log.write_text("\n".join(lines) + "\n")
    return tmp_path


def test_run_requires_three_minute_confirmation_then_kills(tmp_path):
    repo = make_repo(tmp_path, [f"{ts(1)} {BAD}"])
    killed = []
    kw = dict(repo_root=repo, find_pid=lambda: 4242, kill=killed.append)
    assert "confirmation window" in w.run(now=NOW, **kw)
    assert w.run(now=NOW + timedelta(minutes=2), **kw) is None
    assert killed == []
    assert "killed orchestrator (pid 4242)" in w.run(now=NOW + timedelta(minutes=3), **kw)
    assert killed == [4242]
    state = json.loads((repo / "state" / "telegram_watchdog.json").read_text())
    assert state["first_unhealthy_at"] is None and state["last_restart_at"]


def test_run_clears_tracking_on_recovery_and_ignores_missing_process(tmp_path):
    repo = make_repo(tmp_path, [f"{ts(1)} {BAD}"])
    kw = dict(repo_root=repo, kill=lambda p: None)
    w.run(now=NOW, find_pid=lambda: 1, **kw)
    (repo / "state" / "logs" / "orchestrator_debug.log").write_text(f"{ts(0)} {GOOD}\n")
    assert "recovered" in w.run(now=NOW + timedelta(minutes=1), find_pid=lambda: 1, **kw)
    assert w.run(now=NOW, find_pid=lambda: None, **kw) is None
