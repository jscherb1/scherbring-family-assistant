import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import scheduler_dispatch as sd  # noqa: E402


def test_denials_are_counted_from_the_debug_log(tmp_path):
    log = tmp_path / "run.log"
    log.write_text(
        "2026 [DEBUG] Bash tool permission denied\n"
        "2026 [DEBUG] something else\n"
        "2026 [DEBUG] Write tool permission denied\n"
    )
    assert sd.count_permission_denials(log) == 2


def test_clean_or_missing_log_means_zero(tmp_path):
    clean = tmp_path / "clean.log"
    clean.write_text("all fine\n")
    assert sd.count_permission_denials(clean) == 0
    assert sd.count_permission_denials(tmp_path / "missing.log") == 0


def _git(repo, *args):
    import subprocess
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_checkout_problem_names_branch_and_dirty_state(tmp_path):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "f").write_text("a")
    _git(tmp_path, "add", "f")
    _git(tmp_path, "commit", "-m", "x")
    assert sd.checkout_problem(tmp_path) == ""
    (tmp_path / "f").write_text("b")
    assert "uncommitted" in sd.checkout_problem(tmp_path)
    _git(tmp_path, "checkout", "-q", "-b", "feat/x")
    assert "feat/x" in sd.checkout_problem(tmp_path)


def test_checkout_problem_is_empty_outside_git(tmp_path):
    assert sd.checkout_problem(tmp_path) == ""


def test_denied_tool_names_are_listed_in_order(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("[DEBUG] Bash tool permission denied\n[DEBUG] Write tool permission denied\n")
    assert sd.denied_tool_names(log) == ["Bash", "Write"]
    assert sd.denied_tool_names(tmp_path / "missing.log") == []


# --- JSON output, degraded status ---------------------------------------------------------------

import json  # noqa: E402
import subprocess  # noqa: E402


def _result_json(text, denials=()):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": text,
                       "permission_denials": list(denials)})


def test_parse_claude_json_extracts_reply_and_denied_commands():
    raw = _result_json("hello", [{"tool_name": "Bash", "tool_use_id": "t1",
                                  "tool_input": {"command": "cd x && python y.py"}}])
    text, denials, is_error = sd.parse_claude_json(raw)
    assert is_error is False
    assert text == "hello"
    assert denials[0]["tool_name"] == "Bash" and denials[0]["tool_input"]["command"] == "cd x && python y.py"


def test_parse_claude_json_falls_back_on_garbage():
    assert sd.parse_claude_json("just some plain reply") is None
    assert sd.parse_claude_json("") is None
    assert sd.parse_claude_json(json.dumps({"no": "result"})) is None
    assert sd.parse_claude_json(json.dumps([1, 2])) is None


def test_parse_claude_json_accepts_event_array():
    raw = json.dumps([{"type": "system"}, json.loads(_result_json("hi"))])
    assert sd.parse_claude_json(raw) == ("hi", [], False)


def test_denial_summary_truncates():
    d = {"tool_name": "Bash", "tool_input": {"command": "x" * 300}}
    assert len(sd.describe_denial(d)) < 130 and sd.describe_denial(d).endswith("...")
    many = [{"tool_name": "Bash", "tool_input": {"command": f"c{i}"}} for i in range(5)]
    assert sd.summarize_denials(many).endswith("+2 more")


def _run_dispatch(monkeypatch, tmp_path, stdout, returncode=0, delivered=True, debug_text=""):
    monkeypatch.setattr(sd, "DISPATCH_DEBUG_DIR", tmp_path)
    monkeypatch.setattr(sd, "checkout_problem", lambda *a, **k: "")
    sent = []

    def fake_run(args, **kw):
        (tmp_path / "t1_2026-10-31T09-00-00.log").write_text(debug_text)
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(sd.subprocess, "run", fake_run)
    monkeypatch.setattr(sd, "deliver_telegram_message",
                        lambda chat, text, name="": sent.append(text) or delivered)
    task = {"id": "t1", "name": "task", "prompt": "do it", "target_chat_id": "42", "due_run_at": "2026-10-31T09:00:00"}
    return sd.dispatch_task(task), sent


def _marked(text):
    return f"{sd.TELEGRAM_SEND_START}{text}{sd.TELEGRAM_SEND_END}"


def test_dispatch_passes_json_output_format(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(sd, "DISPATCH_DEBUG_DIR", tmp_path)
    monkeypatch.setattr(sd.subprocess, "run",
                        lambda args, **kw: (seen.setdefault("args", args), subprocess.CompletedProcess(args, 0, stdout="", stderr=""))[1])
    sd.dispatch_task({"id": "t1", "name": "n", "prompt": "p", "due_run_at": "r"})
    assert seen["args"][seen["args"].index("--output-format") + 1] == "json"


def test_clean_json_run_is_ok_and_delivers_reply(monkeypatch, tmp_path):
    (status, summary), sent = _run_dispatch(monkeypatch, tmp_path, _result_json(_marked("all good")))
    assert status == "ok" and sent == ["all good"] and summary == "all good"


def test_denials_with_delivery_are_degraded_and_name_the_command(monkeypatch, tmp_path):
    out = _result_json(_marked("done"), [{"tool_name": "Bash", "tool_input": {"command": "echo $?"}}])
    (status, summary), sent = _run_dispatch(monkeypatch, tmp_path, out)
    assert status == "degraded" and sent == ["done"]
    assert "Bash: echo $?" in summary and "done" in summary


def test_denials_with_did_not_complete_reply_stay_failed(monkeypatch, tmp_path):
    out = _result_json(_marked("I did NOT complete the review."),
                       [{"tool_name": "Bash", "tool_input": {"command": "sed -i x"}}])
    (status, _), _sent = _run_dispatch(monkeypatch, tmp_path, out)
    assert status == "failed"


def test_denials_with_failed_delivery_stay_failed(monkeypatch, tmp_path):
    out = _result_json(_marked("done"), [{"tool_name": "Bash", "tool_input": {"command": "x"}}])
    (status, summary), _ = _run_dispatch(monkeypatch, tmp_path, out, delivered=False)
    assert status == "failed" and "delivery failed" in summary


def test_nonzero_exit_is_failed(monkeypatch, tmp_path):
    (status, _), _ = _run_dispatch(monkeypatch, tmp_path, _result_json("x"), returncode=1)
    assert status == "failed"


def test_unparseable_stdout_falls_back_to_raw_text_and_debug_log(monkeypatch, tmp_path):
    (status, summary), sent = _run_dispatch(
        monkeypatch, tmp_path, _marked("raw reply"), debug_text="Bash tool permission denied\n")
    assert sent == ["raw reply"]  # reply text never lost
    assert status == "degraded" and "Bash" in summary and "raw reply" in summary


def test_unparseable_clean_stdout_is_ok(monkeypatch, tmp_path):
    (status, _), sent = _run_dispatch(monkeypatch, tmp_path, _marked("raw reply"))
    assert status == "ok" and sent == ["raw reply"]


def test_main_sends_no_failure_alert_for_degraded(monkeypatch):
    logged, alerts = [], []
    monkeypatch.setattr(sd, "update_heartbeat", lambda: None)
    monkeypatch.setattr(sd, "get_due_tasks", lambda: [{"id": "t1", "name": "n", "due_run_at": "r"}])
    monkeypatch.setattr(sd, "mark_dispatched", lambda *a: True)
    monkeypatch.setattr(sd, "dispatch_task", lambda t: ("degraded", "denied"))
    monkeypatch.setattr(sd, "log_run", lambda *a: logged.append(a))
    monkeypatch.setattr(sd, "send_failure_alert", lambda *a: alerts.append(a))
    sd.main()
    assert logged[0][2] == "degraded" and alerts == []
    monkeypatch.setattr(sd, "dispatch_task", lambda t: ("failed", "boom"))
    sd.main()
    assert len(alerts) == 1


def test_parse_claude_json_tolerates_warning_lines():
    obj = _result_json("hi", [{"tool_name": "Bash", "tool_input": {"command": "x"}}])
    for raw in (f"warning: something\n{obj}", f"{obj}\n", f"noise {obj} trailing", f"a\n\n{obj}\n\n"):
        text, denials, _ = sd.parse_claude_json(raw)
        assert text == "hi" and len(denials) == 1


def test_parse_claude_json_error_flag_and_missing_result():
    text, _, is_error = sd.parse_claude_json(json.dumps({"type": "result", "result": "API error", "is_error": True}))
    assert is_error and text == "API error"
    text, _, is_error = sd.parse_claude_json(json.dumps(
        {"type": "result", "subtype": "error_max_turns", "is_error": True, "errors": ["too many turns"],
         "permission_denials": []}))
    assert is_error and "error_max_turns" in text and "too many turns" in text and "{" not in text


def test_is_error_result_with_exit_zero_is_failed(monkeypatch, tmp_path):
    out = json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": _marked("hello")})
    (status, _), sent = _run_dispatch(monkeypatch, tmp_path, out)
    assert status == "failed" and sent == []


def test_silent_run_with_denials_is_failed(monkeypatch, tmp_path):
    out = _result_json("nothing is due today", [{"tool_name": "Bash", "tool_input": {"command": "cd x && y"}}])
    (status, summary), sent = _run_dispatch(monkeypatch, tmp_path, out)
    assert status == "failed" and sent == [] and "cd x && y" in summary


def test_silent_run_without_denials_is_ok(monkeypatch, tmp_path):
    (status, _), sent = _run_dispatch(monkeypatch, tmp_path, _result_json("nothing due"))
    assert status == "ok" and sent == []


def test_reply_failure_wording_detection():
    for text in ("COROS workout sync failed: login expired", "The quarterly review did not complete.",
                 "Couldn't complete the upload", "Drive upload failed to deliver", "weather task failed"):
        assert sd.reply_says_failed(text), text
    for text in ("Review: one failed payment of $40 at Netflix", "A check failed earlier and recovered.",
                 "All good. Rain likely Tuesday."):
        assert not sd.reply_says_failed(text), text


def test_delivered_failure_reply_with_denials_stays_failed(monkeypatch, tmp_path):
    out = _result_json(_marked("COROS workout sync failed: login expired"),
                       [{"tool_name": "Bash", "tool_input": {"command": "echo $?"}}])
    (status, _), sent = _run_dispatch(monkeypatch, tmp_path, out)
    assert status == "failed" and sent  # delivered, but reports failure


def test_false_positive_text_with_denials_is_degraded(monkeypatch, tmp_path):
    out = _result_json(_marked("Spending: one failed payment at Netflix"),
                       [{"tool_name": "Bash", "tool_input": {"command": "echo $?"}}])
    (status, _), _ = _run_dispatch(monkeypatch, tmp_path, out)
    assert status == "degraded"
