import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import recipes  # noqa: E402
import recipes_store as rs  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(rs, "DB_PATH", path)
    return path


def add(db, title="Stew", **kw):
    return recipes.add_recipe(title, kw.pop("ingredients", ["beef"]), kw.pop("steps", ["cook"]), db_path=db, **kw)


def test_add_validates_and_defaults(db):
    r = add(db, " Stew ", prep_time_min="10", cook_time_min=20)
    assert r["title"] == "Stew" and r["meal_type"] == "dinner" and r["total_time_min"] == 30
    assert r["ingredients_json"] == [{"text": "beef", "include_in_shopping_list": True}]
    for bad in ({"title": " "}, {"prep_time_min": "x"}, {"servings": -1}, {"steps": "nope"}, {"tags": [1]}):
        with pytest.raises(recipes.RecipeError) as exc:
            add(db, **({"title": "ok"} | bad))
        assert exc.value.code == 2


def test_update_is_partial_and_can_clear(db):
    rid = add(db, description="d", notes="n", prep_time_min=10, cook_time_min=20)["id"]
    r = recipes.update_recipe(rid, title="New", db_path=db)
    assert r["title"] == "New" and r["description"] == "d" and r["total_time_min"] == 30
    r = recipes.update_recipe(rid, cook_time_min=40, description=None, db_path=db)
    assert r["description"] is None and r["notes"] == "n" and r["total_time_min"] == 50
    r = recipes.update_recipe(rid, total_time_min=99, db_path=db)
    assert r["total_time_min"] == 99
    with pytest.raises(recipes.RecipeError) as exc:
        recipes.update_recipe(rid, title="", db_path=db)
    assert exc.value.code == 2 and recipes.get_recipe(rid, db_path=db)["title"] == "New"


def test_delete_and_not_found(db):
    rid = add(db)["id"]
    assert recipes.delete_recipe(rid, db_path=db)["id"] == rid
    for call in (lambda: recipes.get_recipe(rid, db_path=db), lambda: recipes.delete_recipe(rid, db_path=db),
                 lambda: recipes.update_recipe(rid, title="x", db_path=db),
                 lambda: recipes.mark_cooked(rid, "2026-10-01", db_path=db)):
        with pytest.raises(recipes.RecipeError) as exc:
            call()
        assert exc.value.code == 1


def test_mark_cooked_and_feedback_rules(db):
    rid = add(db)["id"]
    assert recipes.mark_cooked(rid, "2026-10-01", db_path=db)["last_cooked_at"] == "2026-10-01"
    with pytest.raises(recipes.RecipeError):
        recipes.mark_cooked(rid, "10/01/2026", db_path=db)
    r = recipes.add_feedback(rid, "too dry", 2, db_path=db)
    assert r["rating"] == 2 and r["feedback_json"][0]["comment"] == "too dry"
    assert recipes.add_feedback(rid, "ok though", db_path=db)["rating"] == 2
    for bad in (6, 0):
        with pytest.raises(recipes.RecipeError):
            recipes.add_feedback(rid, "x", bad, db_path=db)


def test_text_helpers():
    assert recipes.ingredients_from_text("a\n\n b (pantry) \n(pantry)") == [
        {"text": "a", "include_in_shopping_list": True},
        {"text": "b", "include_in_shopping_list": False},
        {"text": "(pantry)", "include_in_shopping_list": True}]
    assert recipes.tags_from_text("x, ,y") == ["x", "y"]


# ---- CLI ----

def run(capsys, *argv):
    code = rs.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_add_update_delete_roundtrip(db, capsys):
    code, out, _ = run(capsys, "add", "--title", "Soup", "--ingredients-json", '["water"]',
                       "--steps-json", '["boil"]', "--notes", "hot")
    rid = json.loads(out)["id"]
    assert code == 0
    code, out, _ = run(capsys, "update", "--id", rid, "--title", "Soup 2", "--tags-json", '["Quick"]',
                       "--clear", "notes")
    row = json.loads(out)
    assert code == 0 and row["title"] == "Soup 2" and row["notes"] is None and row["tags_json"] == ["Quick"]
    code, out, _ = run(capsys, "delete", "--id", rid)
    assert code == 0 and json.loads(out)["deleted"] == rid
    code, _, err = run(capsys, "get", "--id", rid)
    assert code == 1 and "no recipe" in json.loads(err)["error"]


def test_cli_bad_input_exit_codes(db, capsys):
    code, _, err = run(capsys, "add", "--title", "T", "--ingredients-json", "{oops", "--steps-json", "[]")
    assert code == 2 and "not valid JSON" in json.loads(err)["error"]
    code, _, _ = run(capsys, "add", "--title", " ", "--ingredients-json", "[]", "--steps-json", "[]")
    assert code == 2
    code, _, _ = run(capsys, "update", "--id", "nope", "--title", "x")
    assert code == 1
    rid = json.loads(run(capsys, "add", "--title", "T", "--ingredients-json", "[]", "--steps-json", "[]")[1])["id"]
    assert run(capsys, "update", "--id", rid)[0] == 2
    assert run(capsys, "delete", "--id", "nope")[0] == 1
