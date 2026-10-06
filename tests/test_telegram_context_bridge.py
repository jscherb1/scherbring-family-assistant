import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import telegram_context_bridge as tcb  # noqa: E402


def entry(age_h, text="x", direction="in"):
    ts = (datetime.now(timezone.utc) - timedelta(hours=age_h)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"ts": ts, "direction": direction, "chat_id": "1", "text": text}


def test_keeps_recent_drops_old_beyond_floor():
    old = [entry(100 + i, f"old{i}") for i in range(60)][::-1]  # chronological: oldest first
    recent = [entry(1, f"new{i}") for i in range(5)]
    kept = tcb._prune(old + recent)
    assert len(kept) == tcb.MIN_ENTRIES  # floor of 40 beats the 48h window for old entries
    assert [e["text"] for e in kept][-5:] == [f"new{i}" for i in range(5)]


def test_quiet_period_does_not_empty_context():
    entries = [entry(30 * 24 + i, f"m{i}") for i in range(10)][::-1]  # all a month old
    assert len(tcb._prune(entries)) == 10


def test_everything_within_48h_is_kept_even_beyond_floor():
    entries = [entry(40 - i * 0.1, f"m{i}") for i in range(100)]
    assert len(tcb._prune(entries)) == 100


def test_size_cap_trims_only_beyond_floor():
    big = "y" * 20_000
    entries = [entry(10 - i * 0.01, big) for i in range(60)]
    kept = tcb._prune(entries)
    assert tcb.MIN_ENTRIES <= len(kept) < 60
    assert sum(len(json.dumps(e)) for e in kept[-tcb.MIN_ENTRIES:]) > 0


def test_export_legacy_appends_and_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(tcb, "LOG_FILE", tmp_path / "log.jsonl")
    tcb.export_legacy("in", "1", "hello")
    tcb.export_legacy("out", "1", "hi there")
    lines = [json.loads(l) for l in (tmp_path / "log.jsonl").read_text().splitlines()]
    assert [(l["direction"], l["text"]) for l in lines] == [("in", "hello"), ("out", "hi there")]
    monkeypatch.setattr(tcb, "LOG_FILE", tmp_path / "no" / "such" / "\0bad")
    tcb.export_legacy("in", "1", "x")  # swallowed
