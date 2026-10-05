import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import paths  # noqa: E402


def test_secrets_dir_default_and_override(monkeypatch, tmp_path):
    monkeypatch.delenv("SCHERBRING_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.secrets_dir() == tmp_path / ".config" / "scherbring-assistant"
    monkeypatch.setenv("SCHERBRING_CONFIG_DIR", str(tmp_path / "x"))
    assert paths.secrets_dir() == tmp_path / "x"


def test_telegram_env_path_uses_home_not_userprofile(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("USERPROFILE", raising=False)
    assert paths.telegram_env_path() == tmp_path / ".claude" / "channels" / "telegram" / ".env"
    assert paths.telegram_env_path().is_absolute()


def test_get_env_order_and_parsing(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHERBRING_CONFIG_DIR", str(tmp_path))
    (tmp_path / ".env").write_text('# c\nA="from-secrets"\nB=\nEMPTY=\n')
    extra = tmp_path / "extra.env"
    extra.write_text("B=from-extra\nA=from-extra\n")
    monkeypatch.delenv("A", raising=False)
    monkeypatch.delenv("B", raising=False)
    assert paths.get_env("A", (extra,)) == "from-secrets"
    assert paths.get_env("B", (extra,)) == "from-extra"  # empty value skipped
    assert paths.get_env("NOPE", (extra,)) is None
    monkeypatch.setenv("A", "from-process")
    assert paths.get_env("A", (extra,)) == "from-process"


def test_find_claude_returns_str_or_none():
    result = paths.find_claude()
    assert result is None or os.path.exists(result)


def test_load_env_merges_with_priority_and_warns_on_loose_mode(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SCHERBRING_CONFIG_DIR", str(tmp_path))
    env_file = tmp_path / ".env"
    env_file.write_text("K1=secrets\nK2=secrets\n")
    env_file.chmod(0o644)
    monkeypatch.setenv("K2", "process")
    merged = paths.load_env()
    assert merged["K1"] == "secrets"
    assert merged["K2"] == "process"
    assert "chmod 600" in capsys.readouterr().err
    env_file.chmod(0o600)
    paths.load_env()
    assert capsys.readouterr().err == ""
