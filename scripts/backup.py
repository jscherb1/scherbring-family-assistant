#!/usr/bin/env python3
"""Daily backup of everything the assistant cannot regenerate.

Two archives per run, written to the backup directory:
  data-YYYYMMDD-HHMMSS.tar.gz      SQLite snapshot, Claude memory notes, small state files
  secrets-YYYYMMDD-HHMMSS.tar.age  credentials and sessions, encrypted with age

The secrets archive is encrypted to the public key in
~/.config/scherbring-assistant/backup_age_recipient.txt. The matching private key is NOT
kept on this machine (see docs/OPERATIONS.md), so a stolen backup folder is unreadable.
If no recipient file exists, secrets are skipped and the run is reported as failed.

Retention: the newest 14 days of backups, plus the latest backup of each of the 8 weeks
before that.

Backup directory: $ASSISTANT_BACKUP_DIR, else ~/backups/assistant.

Usage:
    python scripts/backup.py              # run a backup (what the systemd timer does)
    python scripts/backup.py verify       # restore-drill the newest data archive into a temp dir
    python scripts/backup.py init-key     # one-time: make the age key pair (see docs/OPERATIONS.md)
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from paths import REPO_ROOT, claude_home, secrets_dir

DAILY_KEEP = 14
WEEKLY_KEEP = 8
STAMP = "%Y%m%d-%H%M%S"
DB = REPO_ROOT / "state" / "agent_results.db"


def backup_dir() -> Path:
    return Path(os.environ.get("ASSISTANT_BACKUP_DIR") or Path.home() / "backups" / "assistant")


def memory_dir() -> Path:
    mangled = str(REPO_ROOT).replace("/", "-")
    return claude_home() / "projects" / mangled / "memory"


def recipient_file() -> Path:
    return secrets_dir() / "backup_age_recipient.txt"


def snapshot_db(dest: Path) -> None:
    """Consistent copy of the live DB (never a raw file copy), verified before use."""
    src = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    out = sqlite3.connect(dest)
    try:
        src.backup(out)
        result = out.execute("pragma integrity_check").fetchone()[0]
    finally:
        out.close()
        src.close()
    if result != "ok":
        raise RuntimeError(f"database snapshot failed integrity_check: {result}")


def _add_if_exists(tar: tarfile.TarFile, path: Path, arcname: str) -> bool:
    if path.exists():
        tar.add(path, arcname=arcname)
        return True
    return False


def make_data_archive(out: Path, tmp: Path) -> list[str]:
    snap = tmp / "agent_results.db"
    snapshot_db(snap)
    state = REPO_ROOT / "state"
    added = ["agent_results.db"]
    with tarfile.open(out, "w:gz") as tar:
        tar.add(snap, arcname="state/agent_results.db")
        for name in ("credential_check.json", "schema.sql"):
            if _add_if_exists(tar, state / name, f"state/{name}"):
                added.append(name)
        if _add_if_exists(tar, REPO_ROOT / "scripts" / "scheduler.config.json", "scripts/scheduler.config.json"):
            added.append("scheduler.config.json")
        if memory_dir().is_dir() and _add_if_exists(tar, memory_dir(), "claude-memory"):
            added.append(f"claude-memory ({len(list(memory_dir().glob('*.md')))} notes)")
    return added


def secret_paths() -> list[tuple[Path, str]]:
    home = Path.home()
    state = REPO_ROOT / "state"
    return [
        (secrets_dir(), "config/scherbring-assistant"),
        (home / ".monarch-mcp-server", "monarch-mcp-server"),
        (home / ".claude" / "channels" / "telegram", "claude-channels-telegram"),
        (state / "hyvee_session.json", "state/hyvee_session.json"),
        (state / "coros_session.json", "state/coros_session.json"),
        (state / "google", "state/google"),
        (REPO_ROOT / ".mcp.json", "repo/.mcp.json"),
        (REPO_ROOT / ".claude" / "settings.local.json", "repo/settings.local.json"),
    ]


def make_secrets_archive(out: Path, tmp: Path) -> None:
    recipient = recipient_file()
    if not recipient.exists():
        raise RuntimeError(f"no age recipient at {recipient}; secrets were NOT backed up")
    plain = tmp / "secrets.tar"
    with tarfile.open(plain, "w") as tar:
        for path, arc in secret_paths():
            _add_if_exists(tar, path, arc)
    plain.chmod(0o600)
    subprocess.run(["age", "-R", str(recipient), "-o", str(out), str(plain)], check=True)
    out.chmod(0o600)


def _stamp_of(path: Path) -> datetime | None:
    try:
        return datetime.strptime(path.name.split("-", 1)[1].split(".")[0], STAMP)
    except (ValueError, IndexError):
        return None


def prune(directory: Path, now: datetime) -> list[Path]:
    """Delete backups outside the retention window. Returns what was removed."""
    removed: list[Path] = []
    for prefix in ("data-", "secrets-"):
        files = sorted(
            (f for f in directory.glob(prefix + "*") if _stamp_of(f)), key=lambda f: _stamp_of(f), reverse=True
        )
        keep: set[Path] = set()
        cutoff = (now - timedelta(days=DAILY_KEEP)).date()
        weeks: dict[tuple[int, int], Path] = {}
        for f in files:
            when = _stamp_of(f)
            if when.date() > cutoff:
                keep.add(f)
            else:
                weeks.setdefault(tuple(when.isocalendar()[:2]), f)  # newest of each week
        keep.update(list(weeks.values())[:WEEKLY_KEEP])
        for f in files:
            if f not in keep:
                f.unlink()
                removed.append(f)
    return removed


def run_backup(now: datetime | None = None) -> list[str]:
    now = now or datetime.now()
    directory = backup_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime(STAMP)
    lines: list[str] = []
    with tempfile.TemporaryDirectory(prefix="assistant-backup-") as t:
        tmp = Path(t)
        tmp.chmod(0o700)
        data = directory / f"data-{stamp}.tar.gz"
        added = make_data_archive(data, tmp)
        lines.append(f"data: {data.name} ({data.stat().st_size // 1024} KB): {', '.join(added)}")
        secrets = directory / f"secrets-{stamp}.tar.age"
        make_secrets_archive(secrets, tmp)
        lines.append(f"secrets: {secrets.name} ({secrets.stat().st_size // 1024} KB, age-encrypted)")
    removed = prune(directory, now)
    if removed:
        lines.append(f"pruned {len(removed)} old file(s)")
    return lines


def verify() -> list[str]:
    """Restore drill: unpack the newest data archive somewhere harmless and check it."""
    archives = sorted(backup_dir().glob("data-*.tar.gz"))
    if not archives:
        raise RuntimeError(f"no data archives in {backup_dir()}")
    newest = archives[-1]
    with tempfile.TemporaryDirectory(prefix="assistant-restore-") as t:
        with tarfile.open(newest) as tar:
            tar.extractall(t, filter="data")
        db = sqlite3.connect(f"file:{t}/state/agent_results.db?mode=ro", uri=True)
        try:
            integrity = db.execute("pragma integrity_check").fetchone()[0]
            tables = db.execute("select count(*) from sqlite_master where type='table'").fetchone()[0]
            tasks = db.execute("select count(*) from scheduled_tasks").fetchone()[0]
        finally:
            db.close()
        notes = len(list(Path(t, "claude-memory").glob("*.md")))
    if integrity != "ok":
        raise RuntimeError(f"{newest.name}: integrity_check={integrity}")
    return [f"{newest.name}: integrity ok, {tables} tables, {tasks} scheduled tasks, {notes} memory notes"]


def init_key() -> list[str]:
    """Create the age key pair. The private key goes to ~/backup-age-key.txt: move it off this machine."""
    private = Path.home() / "backup-age-key.txt"
    if recipient_file().exists() or private.exists():
        raise RuntimeError("a backup key already exists; delete both files first if you really want a new one")
    old = os.umask(0o077)
    try:
        subprocess.run(["age-keygen", "-o", str(private)], check=True, capture_output=True)
        public = subprocess.run(
            ["age-keygen", "-y", str(private)], check=True, capture_output=True, text=True
        ).stdout
        recipient_file().parent.mkdir(parents=True, exist_ok=True)
        recipient_file().write_text(public)
    finally:
        os.umask(old)
    return [
        f"public key written to {recipient_file()}",
        f"PRIVATE key written to {private}: copy it to your password manager, then delete it from this machine",
    ]


def _alert(message: str) -> None:
    try:
        import telegram_send as ts

        token, chat_id = ts._read_token(), ts._default_chat_id()
        if token and chat_id:
            ts._send_raw(token, chat_id, message)
    except Exception as exc:  # noqa: BLE001 - alerting must not mask the real error
        print(f"backup: could not send Telegram alert: {exc}", file=sys.stderr)


def main(argv: list[str]) -> int:
    action = argv[0] if argv else "run"
    try:
        lines = {"verify": verify, "init-key": init_key}.get(action, run_backup)()
    except Exception as exc:  # noqa: BLE001
        print(f"backup FAILED: {exc}", file=sys.stderr)
        _alert(f"\U0001f534 Daily backup failed: {exc}")
        return 1
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
