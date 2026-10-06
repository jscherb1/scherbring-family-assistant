import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from dashboard.app import app  # noqa: E402
from dashboard.data import recipes, restarts, schedule  # noqa: E402


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A temp state/ dir with the real schema and a little seeded data."""
    monkeypatch.setenv("ASSISTANT_STATE_DIR", str(tmp_path))
    (tmp_path / "logs").mkdir()
    conn = sqlite3.connect(tmp_path / "agent_results.db")
    conn.executescript((ROOT / "state" / "schema.sql").read_text())
    conn.execute(
        "INSERT INTO scheduled_tasks (id,name,prompt,cron_expression,target_chat_id,enabled,created_at,last_dispatched_at) "
        "VALUES ('t1','daily-thing','Do the thing','0 8 * * *','1',1,'2026-01-01T00:00:00','2026-10-05T08:00:00')")
    conn.execute(
        "INSERT INTO scheduled_tasks (id,name,prompt,cron_expression,target_chat_id,enabled,created_at) "
        "VALUES ('t2','paused-thing','Nope','0 9 * * 6','1',0,'2026-01-01T00:00:00')")
    recent = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
    for status in ("ok", "failed"):
        conn.execute("INSERT INTO scheduled_task_runs (task_id,run_at,status,summary) VALUES ('t1',?,?,?)",
                     (recent, status, f"{status} summary"))
    conn.execute(
        "INSERT INTO recipes (id,title,description,ingredients_json,steps_json,tags_json,protein_type,meal_type,"
        "total_time_min,rating,last_cooked_at,feedback_json,source_url) VALUES "
        "('r1','Teriyaki Chicken','Sweet and savory',?,?,?,'chicken','dinner',45,5,'2026-09-21',?,'https://example.com/r')",
        (json.dumps([{"item": "chicken thighs", "quantity": "2", "unit": "lbs", "include_in_shopping_list": True},
                     {"text": "1 cup rice", "include_in_shopping_list": False}]),
         json.dumps(["Cook it.", "Eat it."]), json.dumps(["Crockpot"]),
         json.dumps([{"date": "2026-09-21", "comment": "Loved it", "rating": 5}])))
    conn.execute(
        "INSERT INTO recipes (id,title,ingredients_json,steps_json,meal_type) VALUES "
        "('r2','Plain Oatmeal','[]','[]','breakfast')")
    conn.commit()
    conn.close()
    return tmp_path


def write_restart_fixtures(state, kill=True):
    launch1 = datetime(2026, 10, 6, 13, 0, 0, tzinfo=timezone.utc)
    launch2 = launch1 + timedelta(hours=2)
    exit_local = (launch2 - timedelta(seconds=10)).astimezone()
    (state / "orchestrator_restarts.jsonl").write_text(
        json.dumps({"ts": launch1.strftime("%Y-%m-%dT%H:%M:%SZ"), "session_name": "S1", "reason": "wrapper-start"}) + "\n"
        + json.dumps({"ts": launch2.strftime("%Y-%m-%dT%H:%M:%SZ"), "session_name": "S2", "reason": "loop-restart"}) + "\n")
    (state / "logs" / "orchestrator_exits.log").write_text(
        f"[{exit_local.strftime('%Y-%m-%d %H:%M:%S')}] session=S1 exit_code=137\n")
    if kill:
        pass_ts = (exit_local - timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
        (state / "logs" / "watchdog_2026-10-06.log").write_text(
            f"[{pass_ts}] --- watchdog pass ---\n"
            "watchdog: unhealthy signal seen via log grep, starting 3-minute confirmation window\n"
            "watchdog: persistent broken Telegram connection confirmed - killed orchestrator (pid 42)\n")


# ---- describe_cron ----

@pytest.mark.parametrize("expr,expected", [
    ("0 8 * * *", "Daily at 8:00 AM"),
    ("0 8 * * 0", "Sundays at 8:00 AM"),
    ("30 19 * * 0", "Sundays at 7:30 PM"),
    ("0 9 * 11 6", "Saturdays at 9:00 AM in Nov"),
    ("0 9 * 1,4,7,10 6", "Saturdays at 9:00 AM in Jan, Apr, Jul, Oct"),
    ("0 8 * * 1-5", "Mon, Tue, Wed, Thu, Fri at 8:00 AM"),
    ("0 0 * * *", "Daily at 12:00 AM"),
    ("0 9 1 * *", "Day 1 of the month at 9:00 AM"),
    ("*/5 * * * *", "*/5 * * * *"),
    ("garbage", "garbage"),
])
def test_describe_cron(expr, expected):
    assert schedule.describe_cron(expr) == expected


# ---- schedule ----

def test_list_tasks(state):
    result = schedule.list_tasks()
    names = [t["name"] for t in result["tasks"]]
    assert names == ["daily-thing", "paused-thing"]  # enabled first, disabled last
    daily = result["tasks"][0]
    assert daily["schedule"] == "Daily at 8:00 AM"
    assert daily["next_run"] and daily["next_run"].endswith("08:00:00")
    assert daily["ok_7d"] == 1 and daily["fail_7d"] == 1
    assert len(daily["recent_runs"]) == 2
    assert result["tasks"][1]["next_run"] is None  # paused


def test_list_tasks_missing_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_STATE_DIR", str(tmp_path))
    assert "error" in schedule.list_tasks()


# ---- restarts ----

def test_restart_history_watchdog_cause(state):
    write_restart_fixtures(state)
    result = restarts.get_restart_history()
    assert result["summary"]["total"] == 2
    assert result["summary"]["watchdog_caused"] == 1
    newest, oldest = result["events"]
    assert newest["cause_kind"] == "watchdog" and "log grep" in newest["cause"]
    assert newest["exit_code"] == 137 and newest["exit_code_note"] == "SIGKILL"
    assert newest["downtime_seconds"] == pytest.approx(10, abs=1)
    assert any("killed orchestrator" in line for line in newest["watchdog_lines"])
    assert oldest["cause_kind"] == "wrapper"


def test_restart_history_unexplained_kill(state):
    write_restart_fixtures(state, kill=False)
    newest = restarts.get_restart_history()["events"][0]
    assert newest["cause_kind"] == "killed" and newest["watchdog_lines"] == []


def test_restart_history_no_log(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_STATE_DIR", str(tmp_path))
    assert "error" in restarts.get_restart_history()


# ---- recipes ----

def test_recipe_ingredient_shapes(state):
    full = recipes.get_recipe("r1")
    texts = [i["text"] for i in full["ingredients"]]
    assert texts == ["2 lbs chicken thighs", "1 cup rice"]
    assert full["ingredients"][1]["shopping"] is False
    assert full["feedback"][0]["comment"] == "Loved it"


def test_recipe_missing_fields(state):
    sparse = recipes.get_recipe("r2")
    assert sparse["ingredients"] == [] and sparse["steps"] == [] and sparse["tags"] == []
    assert recipes.get_recipe("nope") is None


def test_recipe_filters(state):
    assert [r["title"] for r in recipes.list_recipes(q="rice")["recipes"]] == ["Teriyaki Chicken"]
    assert [r["title"] for r in recipes.list_recipes(meal_type="breakfast")["recipes"]] == ["Plain Oatmeal"]
    assert [r["title"] for r in recipes.list_recipes(tag="Crockpot")["recipes"]] == ["Teriyaki Chicken"]
    assert recipes.list_recipes(protein="beef")["recipes"] == []
    ranked = recipes.list_recipes(sort="rating")["recipes"]
    assert ranked[0]["title"] == "Teriyaki Chicken"
    assert "chicken" in recipes.list_recipes()["facets"]["proteins"]


# ---- app routes ----

@pytest.fixture
def client(state):
    write_restart_fixtures(state)
    return TestClient(app)


@pytest.mark.parametrize("path", [
    "/health", "/health/schedule", "/health/restarts", "/meals", "/meals/r1", "/meals/r2",
    "/meals/results?q=chicken", "/api/status", "/api/schedule", "/api/restarts",
    "/api/recipes", "/api/recipes/r1", "/api/docs",
])
def test_routes_ok(client, path):
    assert client.get(path).status_code == 200


def test_root_redirects(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/health"


def test_unknown_recipe(client):
    assert client.get("/meals/nope").status_code == 404
    assert client.get("/api/recipes/nope").status_code == 404


def test_api_status_shape(client):
    assert set(client.get("/api/status").json()) == {
        "generated_at", "uptime", "task_runs", "watchdogs", "telegram_fallback"}


def test_branding_and_content(client):
    page = client.get("/health/schedule").text
    assert "Scherbring Family Assistant" in page and "daily-thing" in page
    assert "Daily at 8:00 AM" in page
    detail = client.get("/meals/r1").text
    assert "2 lbs chicken thighs" in detail and "Loved it" in detail
    assert "Teriyaki Chicken" in client.get("/meals/results?q=teriyaki").text
    assert "Plain Oatmeal" not in client.get("/meals/results?q=teriyaki").text


def test_output_is_escaped(state):
    conn = sqlite3.connect(state / "agent_results.db")
    conn.execute("INSERT INTO recipes (id,title,ingredients_json,steps_json) VALUES "
                 "('x','<script>alert(1)</script>','[]','[]')")
    conn.commit()
    conn.close()
    assert "<script>alert(1)</script>" not in TestClient(app).get("/meals/x").text


def test_read_only_no_write_routes(client):
    assert client.post("/meals/r1").status_code == 405
    assert client.delete("/api/recipes/r1").status_code == 405
