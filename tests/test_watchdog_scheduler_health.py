import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import watchdog_scheduler_health as w  # noqa: E402

NOW = datetime(2026, 10, 5, 12, 0, 0).astimezone()


def write_tick(repo, minutes_ago):
    f = repo / "state" / "scheduler_loop_state.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"last_tick": (NOW - timedelta(minutes=minutes_ago)).replace(tzinfo=None).isoformat(timespec="seconds")}))


def harness(repo, **overrides):
    calls = {"dispatch": 0, "restart": 0, "alerts": []}
    kw = dict(
        repo_root=repo,
        dispatch=lambda r: calls.__setitem__("dispatch", calls["dispatch"] + 1),
        restart=lambda: calls.__setitem__("restart", calls["restart"] + 1),
        timer=lambda: (True, NOW - timedelta(minutes=1)),
        alert=calls["alerts"].append,
    )
    kw.update(overrides)
    return calls, kw


def test_fresh_tick_is_healthy_and_corrupt_file_is_unknown(tmp_path):
    write_tick(tmp_path, 3)
    assert w.assess(tmp_path / "state" / "scheduler_loop_state.json", NOW) == (False, None)
    (tmp_path / "state" / "scheduler_loop_state.json").write_text("{not json")
    assert w.assess(tmp_path / "state" / "scheduler_loop_state.json", NOW) == (False, None)


def test_missing_and_stale_are_unhealthy(tmp_path):
    assert w.assess(tmp_path / "nope.json", NOW)[0]
    write_tick(tmp_path, 25)
    assert "stale" in w.assess(tmp_path / "state" / "scheduler_loop_state.json", NOW)[1]


def test_debounce_then_recover_and_alert_when_timer_recent(tmp_path):
    write_tick(tmp_path, 30)
    calls, kw = harness(tmp_path)
    assert "confirmation window" in w.run(now=NOW, **kw)[0]
    assert w.run(now=NOW + timedelta(minutes=2), **kw) == []
    out = w.run(now=NOW + timedelta(minutes=5), **kw)
    assert any("recovering" in l for l in out)
    assert calls["dispatch"] == 1 and calls["restart"] == 1 and len(calls["alerts"]) == 1


def test_silent_recovery_when_machine_was_asleep(tmp_path):
    write_tick(tmp_path, 30)
    calls, kw = harness(tmp_path, timer=lambda: (True, NOW - timedelta(minutes=60)))
    w.run(now=NOW, **kw)
    out = w.run(now=NOW + timedelta(minutes=5), **kw)
    assert calls["alerts"] == [] and calls["restart"] == 1
    assert any("asleep" in l for l in out)


def test_clears_tracking_when_healthy_again(tmp_path):
    write_tick(tmp_path, 30)
    calls, kw = harness(tmp_path)
    w.run(now=NOW, **kw)
    write_tick(tmp_path, 1)
    assert "recovered" in w.run(now=NOW + timedelta(minutes=1), **kw)[0]


def test_alert_when_timer_was_not_running(tmp_path):
    write_tick(tmp_path, 30)
    calls, kw = harness(tmp_path, timer=lambda: (False, None))
    w.run(now=NOW, **kw)
    w.run(now=NOW + timedelta(minutes=5), **kw)
    assert len(calls["alerts"]) == 1 and "not running" in calls["alerts"][0]
