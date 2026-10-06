import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import monarch_result as mr  # noqa: E402


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    """A saved-result file inside an allowed location (repo state/ is faked via REPO_ROOT)."""
    monkeypatch.setattr(mr, "REPO_ROOT", tmp_path)
    (tmp_path / "state").mkdir()

    def make(payload, wrap=True):
        f = tmp_path / "state" / "r.txt"
        f.write_text(json.dumps({"result": json.dumps(payload)} if wrap else payload))
        return str(f)

    return make


def call(capsys, *argv):
    rc = mr.main(list(argv))
    cap = capsys.readouterr()
    return rc, (json.loads(cap.out) if cap.out.strip() else None), cap.err


def test_get_unwraps_result_string_and_follows_indices(state_file, capsys):
    f = state_file({"summary": [{"summary": {"savings": 1234.5}}]})
    rc, out, _ = call(capsys, "get", f, "summary.0.summary.savings")
    assert rc == 0 and out == {"summary.0.summary.savings": 1234.5}


def test_get_unwrapped_json_also_works(state_file, capsys):
    f = state_file({"a": {"b": 2}}, wrap=False)
    assert call(capsys, "get", f, "a.b")[1] == {"a.b": 2}


def test_missing_key_is_a_clean_error_listing_keys(state_file, capsys):
    f = state_file({"alpha": 1, "beta": 2})
    rc, out, err = call(capsys, "get", f, "gamma")
    assert rc == 1 and out is None and "alpha" in err and "Traceback" not in err


def test_sum_filters_and_signs(state_file, capsys):
    data = {"data": [{"amount": -10.5, "direction": "expense"}, {"amount": -4.25, "direction": "expense"},
                     {"amount": 100.0, "direction": "income"}]}
    f = state_file(data)
    assert call(capsys, "sum", f, "--path", "data", "--field", "amount")[1]["sum"] == 85.25
    assert call(capsys, "sum", f, "--field", "amount", "--sign", "neg")[1]["sum"] == -14.75
    out = call(capsys, "sum", f, "--field", "amount", "--where", "direction=income")[1]
    assert out["sum"] == 100.0 and out["rows"] == 1


def test_group_sums_across_months_and_orders_by_size(state_file, capsys):
    rows = [{"name": "Food", "planned": 100, "actual": 90, "month": "m1"},
            {"name": "Food", "planned": 100, "actual": 130, "month": "m2"},
            {"name": "Gas", "planned": 50, "actual": 20, "month": "m1"}]
    out = call(capsys, "group", state_file(rows), "--key", "name", "--fields", "actual,planned")[1]
    assert out["rows"][0] == {"name": "Food", "actual": 220.0, "planned": 200.0}
    assert out["groups"] == 2


def test_top_sorts_and_picks_fields(state_file, capsys):
    data = {"by_category": [{"category": "A", "sum": -5}, {"category": "B", "sum": -50}, {"category": "C", "sum": 3}]}
    out = call(capsys, "top", state_file(data), "--path", "by_category", "--sort", "sum", "--n", "2", "--asc",
               "--fields", "category,sum")[1]
    assert [r["category"] for r in out["rows"]] == ["B", "A"] and out["total_rows"] == 3


def test_shape_summarises_without_values(state_file, capsys):
    out = call(capsys, "shape", state_file({"x": [{"secret_value": "abc"}]}))[1]
    assert out == {"x": {"list": 1, "first": {"secret_value": "str"}}}


def test_refuses_to_read_credentials(tmp_path, capsys):
    secret = Path.home() / ".config" / "scherbring-assistant" / ".env"
    for target in (secret, Path.home() / ".monarch-mcp-server" / "token",
                   Path("/etc/passwd"), tmp_path / "anything.json"):
        rc, out, err = call(capsys, "get", str(target), "x")
        assert rc == 1 and "PermissionError" in err, target


def test_output_is_capped(state_file, capsys):
    rows = [{"name": f"n{i}", "v": i} for i in range(3000)]
    mr.main(["group", state_file(rows), "--key", "name", "--fields", "v"])
    assert "truncated" in capsys.readouterr().out
