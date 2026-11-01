import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import scheduler_run_now as rn  # noqa: E402


@pytest.mark.parametrize("status,code", [("ok", 0), ("degraded", 0), ("failed", 1)])
def test_main_exit_code_follows_dispatch_status(monkeypatch, capsys, status, code):
    task = {"id": "t1", "name": "n", "prompt": "p", "cron_expression": "* * * * *"}

    class _R:
        returncode = 0
        stdout = json.dumps(task)

    logged, alerts = [], []
    monkeypatch.setattr(rn, "_store", lambda *a: _R())
    monkeypatch.setattr(rn.sd, "dispatch_task", lambda t, u=None: (status, "summary"))
    monkeypatch.setattr(rn.sd, "log_run", lambda *a: logged.append(a))
    monkeypatch.setattr(rn.sd, "send_failure_alert", lambda *a: alerts.append(a))
    monkeypatch.setattr(sys, "argv", ["scheduler_run_now.py", "--name", "n"])
    assert rn.main() == code
    assert logged[0][2] == status
    assert len(alerts) == (1 if status == "failed" else 0)
    assert json.loads(capsys.readouterr().out)["status"] == status
