import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import agent_ctl  # noqa: E402


class FakeSystemd:
    def __init__(self, active=()):
        self.active = set(active)
        self.calls = []

    def run(self, *args):
        self.calls.append(args)
        verb, units = args[0], [a for a in args[1:] if not a.startswith("-")]
        if verb == "is-active":
            return subprocess.CompletedProcess(args, 0, stdout="active\n" if units[0] in self.active else "inactive\n")
        if verb in ("start", "enable"):
            self.active.update(units)
        if verb in ("stop", "disable"):
            self.active.difference_update(units)
        return subprocess.CompletedProcess(args, 0, stdout="")


def setup(monkeypatch, active, token_env="TELEGRAM_TEST_BOT_TOKEN", tmux=False):
    sd = FakeSystemd(active)
    monkeypatch.setattr(agent_ctl, "_systemctl", sd.run)
    monkeypatch.setattr(agent_ctl, "_tmux_session_exists", lambda: tmux)
    monkeypatch.setattr(agent_ctl, "get_env", lambda name, *a, **k: {"BRIDGE_TOKEN_ENV": token_env}.get(name))
    return sd


def ns(target, yes=False):
    return argparse.Namespace(target=target, yes=yes)


def test_status_warns_when_both_poll_prod_bot(monkeypatch, capsys):
    setup(monkeypatch, {"assistant-orchestrator", "assistant-bridge"}, token_env="TELEGRAM_BOT_TOKEN")
    assert agent_ctl.cmd_mode(ns("status")) == 1
    assert "fight over updates" in capsys.readouterr().out


def test_status_ok_when_bridge_is_on_test_bot(monkeypatch):
    setup(monkeypatch, {"assistant-orchestrator", "assistant-bridge"})
    assert agent_ctl.cmd_mode(ns("status")) == 0


def test_switch_requires_yes_and_changes_nothing(monkeypatch):
    sd = setup(monkeypatch, {"assistant-orchestrator"})
    assert agent_ctl.cmd_mode(ns("bridge")) == 2
    assert all(c[0] == "is-active" for c in sd.calls)


def test_switch_to_bridge_stops_orchestrator_first(monkeypatch):
    sd = setup(monkeypatch, {"assistant-orchestrator"}, token_env="TELEGRAM_BOT_TOKEN")
    assert agent_ctl.cmd_mode(ns("bridge", yes=True)) == 0
    verbs = [c[0] for c in sd.calls if c[0] != "is-active"]
    assert sd.calls.index(("disable", "--now", "assistant-orchestrator")) < sd.calls.index(("enable", "--now", "assistant-bridge", "assistant-agent-worker"))
    assert "assistant-bridge" in sd.active and "assistant-orchestrator" not in sd.active
    assert verbs[0] == "disable"


def test_bridge_refused_if_old_orchestrator_lingers(monkeypatch):
    sd = setup(monkeypatch, {"assistant-orchestrator"}, tmux=True)
    monkeypatch.setattr(agent_ctl.time, "sleep", lambda s: None)
    monkeypatch.setattr(agent_ctl.time, "time", iter(range(0, 1000)).__next__)
    assert agent_ctl.cmd_mode(ns("bridge", yes=True)) == 1
    assert "assistant-bridge" not in sd.active


def test_switch_back_to_plugin(monkeypatch):
    sd = setup(monkeypatch, {"assistant-bridge", "assistant-agent-worker"})
    assert agent_ctl.cmd_mode(ns("plugin", yes=True)) == 0
    assert sd.active == {"assistant-orchestrator"}
