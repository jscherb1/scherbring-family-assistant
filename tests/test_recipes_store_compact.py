import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import recipes_store as rs  # noqa: E402


def test_list_compact_is_small_and_default_unchanged(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rs, "DB_PATH", tmp_path / "t.db")
    data = {"recipes": [{
        "title": "Test Stew", "description": "x" * 2000, "meal_type": "dinner",
        "protein_type": "beef", "tags": ["Crockpot"],
        "ingredients": [{"name": "beef", "amount": "1 lb"}] * 20, "steps": ["step"] * 20,
    }]}
    f = tmp_path / "r.json"
    f.write_text(json.dumps(data))
    assert rs.main(["import", "--file", str(f)]) == 0
    capsys.readouterr()

    assert rs.main(["list", "--compact"]) == 0
    compact_text = capsys.readouterr().out
    row = json.loads(compact_text)[0]
    assert set(row) == {"id", "title", "meal_type", "protein_type", "total_time_min",
                        "rating", "last_cooked_at", "tags"}
    assert row["title"] == "Test Stew" and row["tags"] == ["Crockpot"]

    assert rs.main(["list"]) == 0
    full_text = capsys.readouterr().out
    full = json.loads(full_text)[0]
    assert "ingredients_json" in full and "description" in full  # default unchanged
    assert len(compact_text) < len(full_text) / 5
