import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import backup  # noqa: E402


def _touch(d: Path, when: datetime, kind="data", ext="tar.gz") -> Path:
    f = d / f"{kind}-{when.strftime(backup.STAMP)}.{ext}"
    f.write_text("x")
    return f


def test_prune_keeps_14_days_and_one_per_week(tmp_path):
    now = datetime(2026, 10, 6, 3, 0)
    for i in range(0, 90):
        _touch(tmp_path, now - timedelta(days=i))
    backup.prune(tmp_path, now)
    left = sorted(tmp_path.glob("data-*"))
    recent = [f for f in left if backup._stamp_of(f).date() > (now - timedelta(days=14)).date()]
    assert len(recent) == 14
    assert 14 + 1 <= len(left) <= 14 + 8 + 1  # dailies plus at most 8 weekly survivors (week overlap)
    assert len(left) < 30


def test_prune_ignores_foreign_files(tmp_path):
    (tmp_path / "notes.txt").write_text("keep me")
    backup.prune(tmp_path, datetime(2026, 10, 6))
    assert (tmp_path / "notes.txt").exists()


@pytest.fixture()
def fake_env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "state").mkdir(parents=True)
    db = sqlite3.connect(repo / "state" / "agent_results.db")
    db.execute("create table scheduled_tasks (id integer)")
    db.execute("insert into scheduled_tasks values (1)")
    db.commit()
    db.close()
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / ".env").write_text("TOKEN=abc")
    monkeypatch.setattr(backup, "REPO_ROOT", repo)
    monkeypatch.setattr(backup, "DB", repo / "state" / "agent_results.db")
    monkeypatch.setattr(backup, "secrets_dir", lambda: secrets)
    monkeypatch.setattr(backup, "memory_dir", lambda: tmp_path / "mem")
    (tmp_path / "mem").mkdir()
    (tmp_path / "mem" / "a.md").write_text("note")
    monkeypatch.setenv("ASSISTANT_BACKUP_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(backup.Path, "home", staticmethod(lambda: tmp_path / "home"))
    return tmp_path, secrets


def test_backup_without_recipient_fails_loudly_but_still_writes_data(fake_env):
    tmp_path, _ = fake_env
    with pytest.raises(RuntimeError, match="no age recipient"):
        backup.run_backup(datetime(2026, 10, 6, 3, 0))
    assert list((tmp_path / "out").glob("data-*.tar.gz"))


@pytest.mark.skipif(not (shutil_which := __import__("shutil").which("age")), reason="age not installed")
def test_roundtrip_with_age_and_verify(fake_env):
    tmp_path, secrets = fake_env
    key = tmp_path / "key.txt"
    subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    pub = subprocess.run(["age-keygen", "-y", str(key)], check=True, capture_output=True, text=True).stdout
    (secrets / "backup_age_recipient.txt").write_text(pub)
    lines = backup.run_backup(datetime(2026, 10, 6, 3, 0))
    assert any("age-encrypted" in line for line in lines)
    sec = next((tmp_path / "out").glob("secrets-*.tar.age"))
    assert b"TOKEN=abc" not in sec.read_bytes()
    plain = subprocess.run(["age", "-d", "-i", str(key), str(sec)], check=True, capture_output=True).stdout
    assert b"TOKEN=abc" in plain
    assert "integrity ok" in backup.verify()[0]
