import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import finance_store as fs  # noqa: E402


def run(capsys, *argv):
    rc = fs.main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_who_map_get_single_key_keeps_the_original_contract(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fs, "DB_PATH", tmp_path / "t.db")
    run(capsys, "who-map", "set", "--signal-key", "costco", "--signal-type", "merchant",
        "--who-tag", "WHO - Family", "--confidence", "0.9")
    rc, out, _ = run(capsys, "who-map", "get", "--signal-key", "costco")
    assert rc == 0 and json.loads(out)["who_tag_name"] == "WHO - Family"
    rc, out, err = run(capsys, "who-map", "get", "--signal-key", "nope")
    assert rc != 0 and "no who-map entry" in err


def test_who_map_get_many_keys_in_one_call_misses_are_data(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fs, "DB_PATH", tmp_path / "t.db")
    run(capsys, "who-map", "set", "--signal-key", "costco", "--signal-type", "merchant",
        "--who-tag", "WHO - Family", "--confidence", "0.9")
    run(capsys, "who-map", "set", "--signal-key", "account:1", "--signal-type", "account",
        "--who-tag", "WHO - Justin", "--confidence", "0.8")
    rc, out, _ = run(capsys, "who-map", "get", "--signal-key", "costco", "--signal-key", "target",
                     "--signal-key", "account:1")
    rows = json.loads(out)
    assert rc == 0
    assert [(r["signal_key"], r["found"]) for r in rows] == [("costco", True), ("target", False), ("account:1", True)]
    assert rows[2]["who_tag_name"] == "WHO - Justin" and "who_tag_name" not in rows[1]
