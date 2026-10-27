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
