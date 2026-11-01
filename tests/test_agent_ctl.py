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
