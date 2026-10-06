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
from dashboard.data import activity, family, finance, home, ops, recipes, restarts, schedule, shopping_fitness  # noqa: E402


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
    seed_home(conn)
    seed_family(conn)
    seed_shopping_fitness(conn)
    seed_finance(conn)
    conn.commit()
    conn.close()
    return tmp_path


def seed_finance(conn):
    ts = "2026-10-01T12:00:00"
    rows = (("tagged_auto", "WHO:Justin", "confirmed", "SECRET-MERCHANT", 9876.54),
            ("tagged_confirmed", "Kids", "corrected", "SECRET-MERCHANT", 12.0),
            ("skipped_ambiguous", None, None, "SECRET-MERCHANT", 5.0))
    for i, (action, tag, decision, merchant, amount) in enumerate(rows):
        conn.execute("INSERT INTO finance_tag_log VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)",
                     (f"tl{i}", ts, f"tx{i}", merchant, "a1", amount, "2026-09-30", tag, 0.9, action, decision))
    conn.execute("INSERT INTO finance_who_map VALUES ('costco','merchant','Family',0.9,2,0,'inferred',?)", (ts,))
    conn.execute("INSERT INTO finance_who_map VALUES ('acct','account','Kids',0.3,0,0,'inferred',?)", (ts,))
    conn.execute("INSERT INTO finance_rule_proposals VALUES ('rp1',?,'Target <i>x</i>','Family',NULL,'proposed',NULL,NULL)", (ts,))
    conn.execute("INSERT INTO finance_report_log VALUES ('fr1',?,'weekly','2026-09-21','2026-09-27','d1',"
                 "'https://drive.example/x','SECRET-BRIEF $1,234')", (ts,))
    conn.execute("INSERT INTO finance_report_log VALUES ('fr2',?,'monthly','2026-09-01','2026-09-30','d2','javascript:alert(1)',NULL)", (ts,))
    conn.execute("INSERT INTO finance_config VALUES ('advisor_profile',?)",
                 (json.dumps({"filing_status": "SECRET-MFJ", "risk_tolerance": "SECRET-HIGH"}),))
    conn.execute("INSERT INTO finance_config VALUES ('retirement_assumptions',?)",
                 (json.dumps({"retirement_age": 62, "retirement_spend": 987654, "scenarios": [{"name": "a"}]}),))
    conn.execute("INSERT INTO agent_results (agent,task,created_at,summary) VALUES ('retirement','t',?,'s')", ("2026-10-02T00:00:00Z",))


def seed_shopping_fitness(conn):
    ts = "2026-10-01T00:00:00"
    for i, (name, d) in enumerate((("Milk <b>2%</b>", "2026-09-01"), ("Milk <b>2%</b>", "2026-09-15"), ("Eggs", "2026-09-15"))):
        conn.execute("INSERT INTO hyvee_purchase_history VALUES (?,?,?,?,?,?)",
                     (f"hp{i}", f"u{i}", name, d, f"o{d}", ts))
    conn.execute("INSERT INTO hyvee_item_prefs VALUES ('milk','p1','u1','Milk','1 gal',0.9,NULL,NULL,0,NULL,'history',3,0,?)", (ts,))
    conn.execute("INSERT INTO hyvee_item_prefs VALUES ('eggs','p2','u2','Eggs','12',0.4,NULL,NULL,0,NULL,'history',1,1,?)", (ts,))
    for i, a in enumerate(("accepted", "accepted", "rejected")):
        conn.execute("INSERT INTO hyvee_feedback_log VALUES (?,?,?,?,?,?,?)", (f"fb{i}", ts, "milk", "p1", a, None, None))
    conn.execute("INSERT INTO hyvee_cart_runs VALUES ('cr1',?,?,?,1,'built')", (ts, json.dumps([{"item": "milk"}, {"item": "eggs"}]),
                 json.dumps([{"item": "milk", "decision": "auto"}, {"item": "eggs", "decision": "flag"}])))
    monday = (datetime.now().date() - timedelta(days=datetime.now().weekday())).isoformat()
    conn.execute("INSERT INTO fitness_workout_library VALUES ('l1','c1','Peloton - 45 min','cycle',NULL,NULL,'45 min',NULL,?,?)", (ts, ts))
    conn.execute("INSERT INTO fitness_weekly_plans VALUES ('fp1',?,'Wed','peloton',NULL,'l1',0,'06:00','created',NULL,NULL,'planned',?)", (monday, ts))
    conn.execute("INSERT INTO fitness_weekly_plans VALUES ('fp2',?,'Mon','run','tempo',NULL,1,'05:30','manual_needed',NULL,NULL,'planned',?)", (monday, ts))


def seed_family(conn):
    ts = "2026-01-01T00:00:00"
    soon = (datetime.now() + timedelta(days=10)).strftime("%m-%d")
    conn.execute("INSERT INTO profile_people VALUES ('pp1','Ruth','child','2022-' || ?,NULL,?,?)", (soon, ts, ts))
    conn.execute("INSERT INTO profile_people VALUES ('pp2','Friend <i>X</i>','friend',NULL,NULL,?,?)", (ts, ts))
    conn.execute("INSERT INTO profile_people_facts VALUES ('f1','pp1','allergy','SECRET-PEANUT',?)", (ts,))
    conn.execute("INSERT INTO profile_facts VALUES ('home_address','SECRET-ADDRESS',?)", (ts,))
    conn.execute("INSERT INTO kid_memories VALUES ('m1',?,?,'exact',?,?,?,'telegram',?,'text',NULL,'synced',NULL)",
                 (ts, "2026-03-04", json.dumps(["Ruth", "Claire"]), "SECRET-RAW", "SECRET-TEXT", json.dumps(["funny"])))
    conn.execute("INSERT INTO kid_memories VALUES ('m2',?,?,'exact',?,?,?,'direct',NULL,'text',NULL,'failed',NULL)",
                 (ts, "2026-03-09", json.dumps(["Claire"]), "x", "y"))


def seed_home(conn):
    old = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
    recent = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
    ts = "2026-01-01T00:00:00"
    for iid, name, cat, interval, active in (("h1", "HVAC filter", "hvac", 90, 1),
                                             ("h2", "Smoke detectors", "safety", 180, 1),
                                             ("h3", "Winterize <b>spigots</b>", "seasonal", 365, 0)):
        conn.execute("INSERT INTO home_maintenance_items VALUES (?,?,?,?,?,NULL,?,?)",
                     (iid, name, cat, interval, active, ts, ts))
    conn.execute("INSERT INTO home_maintenance_completions VALUES ('c1','h1',?,NULL,?)", (old, ts))
    conn.execute("INSERT INTO home_maintenance_completions VALUES ('c2','h2',?,'batteries',?)", (recent, ts))
    year = datetime.now().year
    conn.execute("INSERT INTO lawn_garden_program VALUES ('p1',1,'Pre-emerge','15-0-0','Mid April',1,NULL,NULL)")
    conn.execute("INSERT INTO lawn_garden_program VALUES ('p2',2,'Weed control','Trimec','Late May',12,NULL,NULL)")
    conn.execute("INSERT INTO lawn_garden_treatments VALUES ('t1',?,1,'15-0-0','broadcast','whole yard',?,NULL,?)",
                 (f"{year}-02-01", json.dumps(["dandelion"]), ts))
    conn.execute("INSERT INTO lawn_garden_issues VALUES ('i1','quackgrass','back','active',?,NULL,NULL,?,?)",
                 (f"{year}-01-01", ts, ts))
    conn.execute("INSERT INTO lawn_garden_products VALUES ('pr1','T-Zone','herbicide','Mesotrione',NULL,NULL,0,NULL,?,?)", (ts, ts))
    conn.execute("INSERT INTO weather_config VALUES ('latitude','43.9')")
    conn.execute("INSERT INTO weather_config VALUES ('rain_probability_threshold','60')")
    conn.execute("INSERT INTO weather_alerts VALUES ('w1','rain_cushions',?,1,?)",
                 (datetime.now().strftime("%Y-%m-%d"), ts))


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
    "/home", "/home/lawn", "/home/weather",
    "/api/home/maintenance", "/api/home/lawn", "/api/home/weather",
    "/health/system", "/api/system",
    "/family", "/family/memories", "/api/family/profile", "/api/family/memories",
    "/hyvee", "/fitness", "/api/hyvee", "/api/fitness",
    "/finance", "/finance/reports", "/api/finance/tagging", "/api/finance/reports",
    "/meals/plan", "/api/meals/plan", "/activity", "/activity?agent=retirement", "/api/activity",
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


# ---- home & yard ----

def test_maintenance_buckets(state):
    data = home.get_maintenance()
    by = {i["id"]: i for i in data["items"]}
    assert by["h1"]["bucket"] == "overdue" and by["h1"]["days_until_due"] < 0
    assert by["h2"]["bucket"] == "ok"
    assert by["h3"]["bucket"] == "paused"
    assert data["counts"] == {"overdue": 1, "soon": 0, "ok": 1, "paused": 1}
    assert data["items"][0]["id"] == "h1"  # overdue sorts first


def test_lawn_progress(state):
    data = home.get_lawn()
    assert data["rounds_done"] == 1 and data["rounds_total"] == 2
    assert data["active_issues"] == 1 and data["out_of_stock"] == 1
    assert data["treatments"][0]["targets"] == ["dandelion"]


def test_weather_hides_coordinates(state):
    data = home.get_weather()
    assert [c["key"] for c in data["config"]] == ["rain_probability_threshold"]
    assert data["last_7d"] == 1


def test_home_missing_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_STATE_DIR", str(tmp_path))
    for fn in (home.get_maintenance, home.get_lawn, home.get_weather):
        assert "error" in fn()
    assert TestClient(app).get("/home").status_code == 200


def test_home_output_escaped(client):
    page = client.get("/home").text
    assert "<b>spigots</b>" not in page and "&lt;b&gt;spigots" in page


# ---- logins & backups ----

def test_credentials_states(state):
    now = datetime.now().astimezone()
    (state / "credential_check.json").write_text(json.dumps({
        "hyvee": {"authed_at": (now - timedelta(days=10)).isoformat(), "lifetime_days": 11, "status": "ok"},
        "drive": {"authed_at": (now - timedelta(days=1)).isoformat(), "lifetime_days": 7, "status": "ok"},
        "monarch": {"authed_at": (now - timedelta(days=20)).isoformat(), "lifetime_days": 14, "status": "ok"},
    }))
    by = {s["key"]: s for s in ops.get_credentials()["systems"]}
    assert by["hyvee"]["state"] == "warn" and by["drive"]["state"] == "ok" and by["monarch"]["state"] == "expired"


def test_backups_list_only(tmp_path, monkeypatch, state):
    bdir = tmp_path / "bk"
    bdir.mkdir()
    (bdir / "data-20261005-020000.tar.gz").write_bytes(b"x" * 2048)
    monkeypatch.setenv("ASSISTANT_BACKUP_DIR", str(bdir))
    data = ops.get_backups()
    assert data["count"] == 1 and data["files"][0]["kind"] == "data" and not data["stale"]


def test_heartbeat_and_missing_files(state, monkeypatch, tmp_path):
    assert "error" in ops.get_heartbeat() and "error" in ops.get_credentials()
    monkeypatch.setenv("ASSISTANT_BACKUP_DIR", str(tmp_path / "nope"))
    assert "error" in ops.get_backups()
    (state / "scheduler_loop_state.json").write_text(
        json.dumps({"last_tick": (datetime.now() - timedelta(minutes=45)).isoformat()}))
    assert ops.get_heartbeat()["stale"] is True


# ---- people & family (summaries only) ----

def test_profile_summary(state):
    data = family.get_profile()
    assert data["total"] == 2 and data["upcoming"][0]["name"] == "Ruth"
    assert data["upcoming"][0]["days_until_birthday"] == 10 and data["upcoming"][0]["turning"] is not None
    assert data["missing_birthday"] == ["Friend <i>X</i>"] and data["no_facts"] == ["Friend <i>X</i>"]
    assert [g["key"] for g in data["global_fact_keys"]] == ["home_address"]


def test_memories_summary(state):
    data = family.get_memories()
    assert data["total"] == 2 and data["per_child"] == {"Ruth": 1, "Claire": 2}
    assert data["drive"] == {"synced": 1, "failed": 1} and data["tags"] == [("funny", 1)]


def test_family_pages_never_leak_sensitive_text(client):
    for path in ("/family", "/family/memories", "/api/family/profile", "/api/family/memories"):
        body = client.get(path).text
        assert "SECRET" not in body, path
    assert "<i>X</i>" not in client.get("/family").text


def test_birthday_formats():
    from datetime import date
    assert family._next_birthday("03-01", date(2026, 3, 1)) == (date(2026, 3, 1), None)
    assert family._next_birthday("2020-02-29", date(2026, 3, 2))[1] == 7
    assert family._next_birthday("junk", date(2026, 1, 1)) == (None, None)


# ---- hy-vee & fitness ----

def test_hyvee_summary(state):
    data = shopping_fitness.get_hyvee()
    assert data["top_items"][0]["times"] == 2 and data["history"]["orders"] == 2
    assert (data["auto_prefs"], data["low_prefs"]) == (1, 1)
    assert data["acceptance_rate"] == 67
    assert data["runs"][0]["item_count"] == 2 and data["runs"][0]["auto"] == 1 and data["runs"][0]["flagged"] == 1


def test_fitness_week_sorted_and_flags(state):
    data = shopping_fitness.get_fitness()
    assert [w["day_of_week"] for w in data["week"]] == ["Mon", "Wed"]
    assert data["needs_attention"] == 1 and data["is_current_week"] and data["library_total"] == 1


def test_shopping_fitness_escaped_and_missing(client, tmp_path, monkeypatch):
    assert "<b>2%</b>" not in client.get("/hyvee").text
    monkeypatch.setenv("ASSISTANT_STATE_DIR", str(tmp_path / "none"))
    assert "error" in shopping_fitness.get_hyvee() and "error" in shopping_fitness.get_fitness()
    assert client.get("/fitness").status_code == 200


# ---- finance (summaries only) ----

def test_tagging_summary(state):
    data = finance.get_tagging()
    assert data["total"] == 3 and data["decisions"]["pending"] == 1
    assert data["correction_rate"] == 50
    assert data["who_map_total"] == 2 and data["who_map_low"] == 1 and data["proposals_pending"] == 1


def test_reports_summary(state):
    data = finance.get_reports()
    assert data["advisor_filled"] == ["filing_status", "risk_tolerance"]
    assert data["retirement"] == {"retirement_age": 62} and data["retirement_scenarios"] == 1
    assert set(data["last_by_period"]) == {"weekly", "monthly"} and "retirement" in data["last_run"]


def test_finance_pages_never_leak_sensitive_values(client):
    for path in ("/finance", "/finance/reports", "/api/finance/tagging", "/api/finance/reports"):
        body = client.get(path).text
        for secret in ("SECRET", "9876", "987654", "1,234"):
            assert secret not in body, (path, secret)
    page = client.get("/finance/reports").text
    assert "https://drive.example/x" in page and "javascript:alert" not in page
    assert "<i>x</i>" not in client.get("/finance").text


def test_finance_missing_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_STATE_DIR", str(tmp_path))
    assert "error" in finance.get_tagging() and "error" in finance.get_reports()


# ---- meal insights & activity feed ----

def test_meal_insights(state):
    future = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(state / "agent_results.db")
    conn.execute("INSERT INTO agent_results (agent,task,created_at,summary,detail_json) VALUES "
                 "('meal-planner','plan','2026-10-05T00:00:00Z','s',?)",
                 (json.dumps({"meals": [{"title": "Teriyaki Chicken", "recipe_id": "r1", "date": future},
                                        {"title": "Old", "recipe_id": "r1", "date": "2020-01-01"}]}),))
    conn.commit()
    conn.close()
    data = activity.get_meal_insights()
    assert [m["title"] for m in data["menu"]] == ["Teriyaki Chicken"] and data["menu"][0]["recipe_known"]
    assert data["stale_total"] == 1 and data["stale"][0]["id"] == "r2"  # r1 cooked recently; r2 never
    assert dict(data["by_meal_type"]) == {"dinner": 1, "breakfast": 1}


def test_meal_plan_route_beats_recipe_id(client):
    assert "Menu &amp; Insights" in client.get("/meals/plan").text


def test_activity_hides_sensitive_and_truncates(state):
    conn = sqlite3.connect(state / "agent_results.db")
    conn.execute("INSERT INTO agent_results (agent,task,created_at,summary) VALUES "
                 "('finance','t','2026-10-03T00:00:00Z','SECRET $9,999')")
    conn.execute("INSERT INTO agent_results (agent,task,created_at,summary) VALUES "
                 "('todoist','t','2026-10-04T00:00:00Z',?)", ("word " * 200,))
    conn.commit()
    conn.close()
    feed = {f["agent"]: f for f in activity.get_activity()["feed"]}
    assert "SECRET" not in feed["finance"]["summary"]
    assert len(feed["todoist"]["summary"]) <= activity.SUMMARY_MAX
    assert [f["agent"] for f in activity.get_activity("todoist")["feed"]] == ["todoist"]
    assert "SECRET" not in TestClient(app).get("/activity").text


def test_activity_missing_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_STATE_DIR", str(tmp_path))
    assert "error" in activity.get_activity() and "error" in activity.get_meal_insights()
