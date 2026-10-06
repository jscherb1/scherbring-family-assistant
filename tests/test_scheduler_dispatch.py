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
