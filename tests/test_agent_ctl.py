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


def test_effective_token_env_falls_back_to_unit_environment(monkeypatch):
    sd = FakeSystemd()
    sd.run_orig = sd.run

    def run(*args):
        if args[0] == "show":
            return subprocess.CompletedProcess(args, 0, stdout="PYTHONUNBUFFERED=1 BRIDGE_TOKEN_ENV=TELEGRAM_BOT_TOKEN\n")
        return sd.run_orig(*args)

    monkeypatch.setattr(agent_ctl, "_systemctl", run)
    monkeypatch.setattr(agent_ctl, "get_env", lambda name, *a, **k: None)
    assert agent_ctl._effective_token_env() == "TELEGRAM_BOT_TOKEN"


# ---------------------------------------------------------------- dev chat safety

def _env(monkeypatch, **values):
    import paths

    fake = lambda name, *a, **k: values.get(name)  # noqa: E731
    monkeypatch.setattr(paths, "get_env", fake)
    monkeypatch.setattr(agent_ctl, "get_env", fake)


def test_say_default_skips_dev_chats(tmp_path, monkeypatch):
    import argparse
    from bridge_store import BridgeStore

    _env(monkeypatch, BRIDGE_ALLOWED_CHAT_IDS="-100,1", BRIDGE_DEV_CHAT_IDS="-100")
    monkeypatch.setattr(agent_ctl, "DB", tmp_path / "i.db")
    rc = agent_ctl.cmd_say(argparse.Namespace(text="hi", chat=None, timeout=0.1))
    assert rc == 1  # nobody answers in a unit test
    row = BridgeStore(tmp_path / "i.db")._conn.execute("SELECT session_key FROM inbox").fetchone()
    assert row["session_key"] == "1"
    # An explicit --as still reaches the dev chat.
    agent_ctl.cmd_say(argparse.Namespace(text="dev", chat="-100", timeout=0.1))
    keys = [r["session_key"] for r in BridgeStore(tmp_path / "i.db")._conn.execute("SELECT session_key FROM inbox ORDER BY id")]
    assert keys == ["1", "-100"]


def test_say_with_only_dev_chats_refuses(tmp_path, monkeypatch, capsys):
    import argparse

    _env(monkeypatch, BRIDGE_ALLOWED_CHAT_IDS="-100", BRIDGE_DEV_CHAT_IDS="-100")
    monkeypatch.setattr(agent_ctl, "DB", tmp_path / "i.db")
    assert agent_ctl.cmd_say(argparse.Namespace(text="hi", chat=None, timeout=0.1)) == 2


def test_dev_chat_warnings(monkeypatch):
    _env(monkeypatch)
    assert agent_ctl.dev_chat_warnings() == []  # no dev chat, nothing to check
    _env(monkeypatch, BRIDGE_DEV_CHAT_IDS="-100")
    w = agent_ctl.dev_chat_warnings()
    assert len(w) == 2 and "TELEGRAM_ALERT_CHAT_ID" in w[0] and "DASHBOARD_ASK_CHAT_ID" in w[1]
    _env(monkeypatch, BRIDGE_DEV_CHAT_IDS="-100", TELEGRAM_ALERT_CHAT_ID="1", DASHBOARD_ASK_CHAT_ID="-100")
    w = agent_ctl.dev_chat_warnings()
    assert len(w) == 1 and "DASHBOARD_ASK_CHAT_ID points at a dev chat" in w[0]
    _env(monkeypatch, BRIDGE_DEV_CHAT_IDS="-100", TELEGRAM_ALERT_CHAT_ID="1", DASHBOARD_ASK_CHAT_ID="1")
    assert agent_ctl.dev_chat_warnings() == []


def test_status_lists_every_current_turn_and_warns(tmp_path, monkeypatch, capsys):
    import argparse
    import time

    from bridge_store import BridgeStore

    _env(monkeypatch, BRIDGE_DEV_CHAT_IDS="-100")
    store = BridgeStore(tmp_path / "i.db")
    store.set_current_turn("1", 11, time.time() - 5)
    store.set_current_turn("-100", 12, time.time() - 400)
    monkeypatch.setattr(agent_ctl, "DB", tmp_path / "i.db")
    monkeypatch.setattr(agent_ctl, "_systemctl", lambda *a: subprocess.CompletedProcess(a, 0, stdout="active\n"))
    agent_ctl.cmd_status(argparse.Namespace(json=False))
    out = capsys.readouterr().out
    assert "1: message 11" in out and "-100: message 12" in out
    assert "WARNING" in out and "TELEGRAM_ALERT_CHAT_ID" in out
    agent_ctl.cmd_status(argparse.Namespace(json=True))
    import json as _json
    assert len(_json.loads(capsys.readouterr().out)["current_turns"]) == 2


def test_ask_box_default_skips_dev_chats(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from dashboard.routers import ask

    _env(monkeypatch, BRIDGE_ALLOWED_CHAT_IDS="-100,1", BRIDGE_DEV_CHAT_IDS="-100")
    monkeypatch.setattr(ask, "get_env", lambda name, *a, **k: None)
    assert ask._chat_id() == "1"
