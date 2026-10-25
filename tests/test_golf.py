import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import golf  # noqa: E402
import golf_store  # noqa: E402


@pytest.fixture
def db(tmp_path):
    return tmp_path / "t.db"


def add(db, day, score, rating=70.0, slope=113, course="Test Course", **kw):
    return golf.add_round(day, course, score, rating=rating, slope=slope, mirror=False, db_path=db, **kw)


def test_differential_matches_sheet_rows():
    assert golf.differential(94, 69, 125) == pytest.approx(22.6)
    assert golf.differential(84, 70.7, 131) == pytest.approx(11.47251908)
    assert golf.differential(82, 75.3, 133) == pytest.approx(5.692481203)


def test_handicap_best_8_of_last_20_times_096(db):
    # 25 rounds: score = 80 + i, rating 70, slope 113 -> differential 10 + i
    for i in range(25):
        add(db, f"2026-01-{i + 1:02d}", 80 + i)
    h = golf.get_handicap(db_path=db)
    # window = the 20 newest (i=5..24); best 8 = i 5..12 -> differentials 15..22, mean 18.5, x0.96 = 17.76
    assert h["handicap"] == 17.8
    assert h["rounds_considered"] == 20 and len(h["counted_ids"]) == 8 and not h["provisional"]


def test_handicap_uses_play_date_not_entry_order(db):
    add(db, "2026-06-01", 100)
    add(db, "2026-01-01", 70)  # entered later but played earlier
    rounds = golf.list_rounds(db_path=db)
    assert [r["played_on"] for r in rounds] == ["2026-06-01", "2026-01-01"]


def test_few_rounds_is_provisional(db):
    add(db, "2026-03-01", 90)
    h = golf.get_handicap(db_path=db)
    assert h["provisional"] and h["handicap"] == round(20 * 0.96, 1)


def test_no_rounds(db):
    assert golf.get_handicap(db_path=db)["handicap"] is None


def test_preview_writes_nothing_and_predicts_add(db):
    for i in range(10):
        add(db, f"2026-02-{i + 1:02d}", 90)
    before = golf.get_handicap(db_path=db)["handicap"]
    p = golf.preview_round("2026-03-01", "Test Course", 75, rating=70, slope=113, db_path=db)
    assert len(golf.list_rounds(100, db_path=db)) == 10
    assert p["handicap_before"] == before and p["counts"] is True and p["change"] < 0
    done = golf.add_round("2026-03-01", "Test Course", 75, rating=70, slope=113, mirror=False, db_path=db)
    assert done["handicap_after"] == p["handicap_after"]


def test_course_autofill_and_save(db):
    add(db, "2026-04-01", 90, rating=69, slope=125, course="Oaks - Whites")
    r = golf.add_round("2026-04-10", "oaks", 88, mirror=False, db_path=db)["round"]
    assert (r["rating"], r["slope"], r["course"]) == (69.0, 125, "Oaks - Whites")
    with pytest.raises(golf.GolfError) as e:
        golf.add_round("2026-04-11", "Nowhere", 90, mirror=False, db_path=db)
    assert e.value.code == 1


@pytest.mark.parametrize("kwargs", [
    {"played_on": "nope"}, {"played_on": "2999-01-01"}, {"score": 20}, {"score": 30},  # 30: nine-hole-looking
    {"rating": 30, "score": 36}, {"slope": 300}, {"course": " "},
])
def test_bad_input_rejected(db, kwargs):
    args = {"played_on": "2026-05-01", "course": "X", "score": 90, "rating": 70, "slope": 113} | kwargs
    with pytest.raises(golf.GolfError) as e:
        golf.add_round(args["played_on"], args["course"], args["score"], rating=args["rating"],
                       slope=args["slope"], mirror=False, db_path=db)
    assert e.value.code == 2


def test_undo_delete_update(db):
    a = add(db, "2026-05-01", 90)["round"]["id"]
    add(db, "2026-05-02", 95)
    assert golf.undo_last(db_path=db)["deleted"]["score"] == 95
    upd = golf.update_round(a, db_path=db, score=85)
    assert upd["score"] == 85 and upd["differential"] == pytest.approx(15.0)
    golf.delete_round(a, db_path=db)
    with pytest.raises(golf.GolfError) as e:
        golf.undo_last(db_path=db)
    assert e.value.code == 1


def test_sheet_failure_never_blocks_the_log(db, monkeypatch):
    import golf_sheet

    def boom(*_a, **_k):
        raise RuntimeError("sheets down")

    monkeypatch.setattr(golf_sheet, "append_round", boom)
    out = golf.add_round("2026-05-01", "X", 90, rating=70, slope=113, db_path=db)
    assert out["sheet"]["synced"] is False and "sheets down" in out["sheet"]["error"]
    assert out["round"]["sheet_synced"] is False
    monkeypatch.setattr(golf_sheet, "append_round", lambda *_a, **_k: None)
    assert golf.sync_pending(db_path=db)["synced"] == 1
    assert golf.get_round(out["round"]["id"], db_path=db)["sheet_synced"] is True


def test_import_rows_dedupes_and_saves_courses(db):
    rows = [["5/14/23", "Hiawatha", "70.7", "131", "84"], ["2026-10-17", "Somerby", 75.3, 133, 82],
            ["5/14/23", "Hiawatha", "70.7", "131", "84"], ["bad", "x", 1, 1, 1]]
    out = golf.import_rows(rows, db_path=db)
    assert out["imported"] == 2 and [s["reason"] for s in out["skipped"]][0] == "duplicate"
    assert {c["name"] for c in golf.list_courses(db)} == {"Hiawatha", "Somerby"}
    assert golf.import_rows(rows[:2], db_path=db)["imported"] == 0  # idempotent


def test_cli_roundtrip_and_exit_codes(db, monkeypatch, capsys):
    monkeypatch.setattr(golf, "DB_PATH", db)
    assert golf_store.main(["round", "add", "--date", "2026-05-01", "--course", "Oaks", "--score", "90",
                            "--rating", "69", "--slope", "125", "--no-sheet"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["round"]["score"] == 90
    assert golf_store.main(["handicap"]) == 0
    assert golf_store.main(["round", "add", "--date", "bad", "--course", "Oaks", "--score", "90"]) == 2
    assert golf_store.main(["round", "delete", "--id", "nope"]) == 1
    assert golf_store.main(["round", "undo"]) == 0
