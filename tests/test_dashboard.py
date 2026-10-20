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
    "/habits", "/habits?owner=Justin", "/api/habits", "/habits/analysis", "/api/habits/analysis",
    "/rating", "/rating/insights", "/api/rating", "/api/rating/insights",
    "/wellness", "/wellness/trends", "/wellness/trends?range=30&heat=hrv", "/wellness/trends?range=180",
    "/wellness/drivers", "/wellness/drivers?target=hrv&x=steps&y=hrv", "/api/wellness", "/api/wellness/trends",
    "/api/wellness/heatmap", "/api/wellness/drivers", "/api/wellness/scatter",
    "/wellness/journal", "/wellness/journal?date=2026-10-01", "/api/journal/checkin", "/api/journal/history",
    "/api/journal/questions", "/api/journal/impact",
    "/chores", "/chores/manage", "/chores/ledger", "/kids", "/api/chores", "/api/chores/ledger",
    "/meals/plan", "/api/meals/plan", "/activity", "/activity?agent=retirement", "/api/activity",
])
def test_routes_ok(client, path):
    assert client.get(path).status_code == 200


def test_landing_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Ask or tell the assistant" in r.text
    assert 'rel="manifest"' in r.text and "apple-touch-icon" in r.text
    data = client.get("/api/summary").json()
    assert data["status"] in ("ok", "attention") and len(data["tiles"]) == 11
    assert "SECRET-MERCHANT" not in r.text


def test_manifest_and_icons(client):
    m = client.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.json()["display"] == "standalone"
    for icon in m.json()["icons"]:
        assert client.get(icon["src"]).status_code == 200
    assert client.get("/favicon.ico").status_code == 200
    assert client.get("/static/icons/apple-touch-icon.png").status_code == 200


@pytest.fixture
def ask_client(client, state, monkeypatch):
    from dashboard.routers import ask
    monkeypatch.setenv("DASHBOARD_ASK_CHAT_ID", "4242")
    ask._recent.clear()
    return client


def _inbox_rows(state):
    conn = sqlite3.connect(state / "inbox.db")
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM inbox").fetchall()
    finally:
        conn.close()


def test_ask_queues_telegram_row(ask_client, state):
    r = ask_client.post("/ask", data={"text": "add milk"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?sent=1"
    (row,) = _inbox_rows(state)
    payload = json.loads(row["payload"])
    assert (row["channel"], row["chat_ref"], row["text"], row["kind"], row["status"]) == (
        "telegram", "4242", "add milk", "message", "pending")
    assert payload["via"] == "web" and payload["user"] == "Justin"
    htmx = ask_client.post("/ask", data={"text": "again"}, headers={"HX-Request": "true"})
    assert htmx.status_code == 200 and "Telegram" in htmx.text


@pytest.mark.parametrize("data,headers", [
    ({"text": "   "}, {}),
    ({"text": "x" * 2001}, {}),
    ({"text": "hi"}, {"Origin": "https://evil.example"}),
])
def test_ask_rejects_bad_input(ask_client, state, data, headers):
    r = ask_client.post("/ask", data=data, headers={"HX-Request": "true", **headers})
    assert r.status_code == 400
    assert not (state / "inbox.db").exists() or _inbox_rows(state) == []


def test_api_ask_ios(ask_client, state):
    r = ask_client.post("/api/ask", data={"text": "add eggs"})
    assert r.status_code == 200 and r.json()["ok"] is True
    j = ask_client.post("/api/ask", json={"text": "from json"}, headers={"Origin": "http://elsewhere"})
    assert j.status_code == 200
    rows = _inbox_rows(state)
    assert [r["text"] for r in rows] == ["add eggs", "from json"]
    assert all(json.loads(r["payload"])["via"] == "ios" for r in rows)
    for bad in ({"text": "  "}, {"text": "x" * 2001}, {}):
        assert ask_client.post("/api/ask", data=bad).status_code == 400


def test_ask_rate_limited(ask_client):
    from dashboard.routers import ask
    codes = [ask_client.post("/ask", data={"text": f"m{i}"}, headers={"HX-Request": "true"}).status_code
             for i in range(ask.RATE_LIMIT + 1)]
    assert codes[:-1] == [200] * ask.RATE_LIMIT and codes[-1] == 400


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


def test_read_only_except_allowlisted_write_routes(client):
    """The dashboard is read-only apart from the ask box, the habits section and the chore chart. Adding a
    write route elsewhere must be a deliberate change to this allow-list."""
    assert client.post("/meals/r1").status_code == 405
    assert client.delete("/api/recipes/r1").status_code == 405
    allowed = {"/wellness/checkin", "/journal/questions", "/journal/questions/{slug}/update", "/ask", "/api/ask", "/attention/dismiss", "/attention/restore", "/me/attention/dismiss", "/me/attention/restore", "/me/refresh",
               "/meals", "/meals/{recipe_id}/update", "/meals/{recipe_id}/delete",
               "/habits", "/habits/{habit_id}/log", "/habits/{habit_id}/undo", "/habits/{habit_id}/update", "/rating",
               "/chores/kids", "/chores/kids/{kid_id}/update", "/chores/chores", "/chores/chores/{chore_id}/update",
               "/chores/payout", "/chores/backfill", "/chores/undo",
               "/kids/{kid_id}/chores/{chore_id}/done", "/kids/{kid_id}/chores/{chore_id}/undo",
               "/reconnect/drive/start", "/reconnect/drive/complete"}
    writes = {r.path for r in app.routes
              if getattr(r, "methods", None) and r.methods & {"POST", "PUT", "PATCH", "DELETE"}}
    assert writes <= allowed, f"unexpected write routes: {writes - allowed}"


# ---- habits (shared service: scripts/habits.py) ----

def test_habits_create_log_undo_pause_via_ui(client):
    assert "No habits yet" in client.get("/habits").text
    r = client.post("/habits", data={"name": "Stretch <b>", "frequency": "daily", "target": "1", "owner": "Justin"},
                    follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/habits").text
    assert "Stretch &lt;b&gt;" in page and "Stretch <b>" not in page
    hid = client.get("/api/habits").json()["habits"][0]["id"]

    r = client.post(f"/habits/{hid}/log", headers={"HX-Request": "true"})
    assert r.status_code == 200 and "1/1 today" in r.text
    assert client.get(f"/api/habits/{hid}").json()["current_streak"] == 1

    r = client.post(f"/habits/{hid}/undo", headers={"HX-Request": "true"})
    assert client.get(f"/api/habits/{hid}").json()["total_count"] == 0
    client.post(f"/habits/{hid}/update", data={"active": "0"}, headers={"HX-Request": "true"})
    assert client.get("/api/habits?include_paused=false").json()["habits"] == []
    assert "paused" in client.get("/habits").text


def test_habits_log_for_past_date_via_ui(client):
    from datetime import date, timedelta
    client.post("/habits", data={"name": "Read", "frequency": "daily", "target": "1"}, follow_redirects=False)
    hid = client.get("/api/habits").json()["habits"][0]["id"]
    page = client.get("/habits").text
    assert 'name="log_date"' in page and "data-pick=" in page
    yday = (date.today() - timedelta(days=1)).isoformat()
    r = client.post(f"/habits/{hid}/log", data={"log_date": yday}, headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert client.get(f"/api/habits/{hid}").json()["last_done"] == yday
    future = (date.today() + timedelta(days=2)).isoformat()
    r = client.post(f"/habits/{hid}/log", data={"log_date": future}, headers={"HX-Request": "true"})
    assert client.get(f"/api/habits/{hid}").json()["total_count"] == 1


def test_habits_ui_and_service_share_data(client, state):
    """A habit created through the agent CLI's service shows in the UI, and vice versa."""
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import habits as service
    hid = service.create_habit("Read", "weekly", 3, db_path=state / "agent_results.db")["id"]
    assert any(h["id"] == hid for h in client.get("/api/habits").json()["habits"])
    client.post(f"/habits/{hid}/log", headers={"HX-Request": "true"})
    assert service.get_status(hid, db_path=state / "agent_results.db")["done_this_period"] == 1


def test_habits_calendar_and_analysis_share_service_data(client, state):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import habits as service
    db = state / "agent_results.db"
    hid = service.create_habit("Stretch", "daily", 1, db_path=db)["id"]
    service.log_completion(hid, db_path=db)
    cal = client.get(f"/api/habits/{hid}/calendar").json()
    assert cal == service.calendar(hid, db_path=db) and cal["days_done"] == 1
    assert 'data-flip' in client.get("/habits").text and "hcal-day on" in client.get("/habits").text
    ana = client.get("/api/habits/analysis").json()
    assert ana["habits"][0]["days_done"] == 1 and ana["active_days"] == 1
    assert "Habit analysis" in client.get("/habits/analysis").text
    assert client.get("/api/habits/nope/calendar").status_code == 404


def test_habits_write_errors_and_origin_guard(client):
    assert client.post("/habits", data={"name": " ", "frequency": "daily"}, follow_redirects=False).status_code == 303
    r = client.post("/habits", data={"name": "x", "frequency": "daily", "target": "abc"}, follow_redirects=False)
    assert "whole+number" in r.headers["location"]
    r = client.post("/habits/nope/log", headers={"HX-Request": "true"})
    assert r.status_code == 200 and "no habit" in r.text
    assert client.get("/api/habits/nope").status_code == 404
    blocked = client.post("/habits", data={"name": "x", "frequency": "daily"},
                          headers={"Origin": "https://evil.example"}, follow_redirects=False)
    assert blocked.status_code == 403
    assert client.get("/api/habits").json()["habits"] == []


def test_habits_summary_tile_flags_at_risk(client, state):
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import habits as service
    from datetime import date, timedelta
    db = state / "agent_results.db"
    hid = service.create_habit("Floss", "daily", 1, db_path=db)["id"]
    yday = date.today() - timedelta(days=1)
    service.log_completion(hid, log_date=yday.isoformat(), db_path=db)
    data = client.get("/api/summary").json()
    tile = next(t for t in data["tiles"] if t["title"] == "Habits")
    assert tile["headline"] == "1 at risk"
    assert any(a["text"].startswith("Floss streak") for a in data["attention"])


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
        "drive": {"authed_at": (now - timedelta(days=1)).isoformat(), "lifetime_days": 7, "status": "ok"},
        "monarch": {"authed_at": (now - timedelta(days=20)).isoformat(), "lifetime_days": 14, "status": "ok"},
    }))
    by = {s["key"]: s for s in ops.get_credentials()["systems"]}
    assert by["hyvee"]["state"] == "unknown"  # no connection check recorded in this test state
    assert by["drive"]["state"] == "ok" and by["monarch"]["state"] == "expired"


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


def _seed_recent_failure(state):
    conn = sqlite3.connect(state / "agent_results.db")
    run_at = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None).isoformat(timespec="seconds")
    conn.execute("INSERT INTO scheduled_task_runs (task_id,run_at,status,summary) VALUES ('t1',?,?,?)",
                 (run_at, "failed", "boom"))
    conn.commit()
    conn.close()
    return run_at


def _sched_items(client):
    return [a for a in client.get("/api/summary").json()["attention"] if a["key"].startswith("sched:")]


def test_dismiss_and_restore_attention(client, state):
    _seed_recent_failure(state)
    (item,) = _sched_items(client)
    assert item["text"].startswith("daily-thing failed")
    r = client.post("/attention/dismiss", data={"key": item["key"]}, follow_redirects=False)
    assert r.status_code == 303
    assert _sched_items(client) == []
    page = client.get("/")
    assert "1 dismissed" in page.text and item["text"] in page.text  # restorable
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import myhub as service
    assert item["key"] in service.dismissed_rows(db_path=state / "agent_results.db")  # one shared store
    client.post("/attention/restore", data={"key": item["key"]})
    assert len(_sched_items(client)) == 1


def test_new_failure_shows_after_dismissal(client, state):
    first = _seed_recent_failure(state)
    (item,) = _sched_items(client)
    client.post("/attention/dismiss", data={"key": item["key"]})
    conn = sqlite3.connect(state / "agent_results.db")
    later = (datetime.fromisoformat(first) + timedelta(minutes=5)).isoformat(timespec="seconds")
    conn.execute("INSERT INTO scheduled_task_runs (task_id,run_at,status,summary) VALUES ('t1',?,?,?)",
                 (later, "failed", "boom again"))
    conn.commit()
    conn.close()
    (new_item,) = _sched_items(client)
    assert new_item["key"] != item["key"]


def test_dismiss_blocks_cross_origin(client):
    r = client.post("/attention/dismiss", data={"key": "x"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


# ---- chores (shared service: scripts/chores.py) ----

@pytest.fixture
def chores_svc(state):
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import chores as service
    return service, state / "agent_results.db"


def _seed_chart(chores_svc):
    service, db = chores_svc
    kid = service.add_kid("Ruth <b>", db_path=db)["id"]
    chore = service.create_chore("Feed dog", "daily", value_cents=50, kids=["Ruth"], db_path=db)["id"]
    return service, db, kid, chore


def test_kids_page_tap_done_and_undo_via_ui(client, chores_svc):
    service, db, kid, chore = _seed_chart(chores_svc)
    page = client.get("/kids").text
    assert "Ruth &lt;b&gt;" in page and "Ruth <b>" not in page and "Feed dog" in page

    r = client.post(f"/kids/{kid}/chores/{chore}/done", headers={"HX-Request": "true"})
    assert r.status_code == 200 and "1 of 1 done" in r.text and "$0.50" in r.text
    trigger = json.loads(r.headers["hx-trigger"])["chore-celebrate"]
    assert trigger["earned"] == "$0.50" and trigger["all_done"] is True
    assert service.balance(kid, db_path=db) == 50

    again = client.post(f"/kids/{kid}/chores/{chore}/done", headers={"HX-Request": "true"})
    assert again.status_code == 200 and "already done" in again.text and "hx-trigger" not in again.headers
    assert service.balance(kid, db_path=db) == 50

    r = client.post(f"/kids/{kid}/chores/{chore}/undo", headers={"HX-Request": "true"})
    assert "0 of 1 done" in r.text and service.balance(kid, db_path=db) == 0


def test_kid_solo_pages_show_only_that_kid(client, chores_svc):
    service, db = chores_svc
    ruth = service.add_kid("Ruth", db_path=db)["id"]
    claire = service.add_kid("Claire", db_path=db)["id"]
    rc = service.create_chore("Feed dog", "daily", value_cents=50, kids=["Ruth"], db_path=db)["id"]
    service.create_chore("Practice piano", "daily", value_cents=25, kids=["Claire"], db_path=db)
    both = client.get("/kids").text
    assert "Ruth" in both and "Claire" in both
    r = client.get("/kids/RUTH").text
    assert "Ruth&#39;s Chores" in r and "Feed dog" in r
    assert "Claire" not in r and "Practice piano" not in r and claire not in r
    c = client.get("/kids/claire").text
    assert "Practice piano" in c and "Ruth" not in c and "Feed dog" not in c and ruth not in c
    assert client.get("/kids/nobody").status_code == 404
    assert client.get("/kids/r").status_code == 404
    # tapping from the solo page still works through the shared write route
    done = client.post(f"/kids/{ruth}/chores/{rc}/done", headers={"HX-Request": "true"})
    assert "1 of 1 done" in done.text and service.balance(ruth, db_path=db) == 50


def test_kids_routes_origin_guard_and_unknown_ids(client, chores_svc):
    service, db, kid, chore = _seed_chart(chores_svc)
    blocked = client.post(f"/kids/{kid}/chores/{chore}/done", headers={"Origin": "https://evil.example"})
    assert blocked.status_code == 403 and service.balance(kid, db_path=db) == 0
    r = client.post(f"/kids/nope/chores/{chore}/done", headers={"HX-Request": "true"})
    assert r.status_code == 200 and "no kid" in r.text


def test_chores_ui_and_service_share_data(client, chores_svc):
    """Everything created through the service (agent CLI path) shows in the UI/API, and UI writes land in the service."""
    service, db, kid, chore = _seed_chart(chores_svc)
    api = client.get("/api/chores").json()
    assert api["kids"][0]["todo"][0]["name"] == "Feed dog"
    client.post(f"/kids/{kid}/chores/{chore}/done", headers={"HX-Request": "true"})
    assert client.get(f"/api/chores/kids/{kid}").json()["balance_cents"] == 50
    r = client.post("/chores/payout", data={"kid_id": kid, "amount": "0.25", "note": "cash"}, follow_redirects=False)
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    assert service.balance(kid, db_path=db) == 25
    assert any(e["kind"] == "payout" for e in client.get("/api/chores/ledger").json()["kids"][0]["ledger"])
    assert client.get("/api/chores/kids/nope").status_code == 404


def test_chores_adult_config_writes(client, chores_svc):
    service, db = chores_svc
    assert client.post("/chores/kids", data={"name": "Claire"}, follow_redirects=False).status_code == 303
    claire = service.resolve_kid("Claire", db)["id"]
    r = client.post("/chores/chores", data={"name": "Trash", "frequency": "weekly", "days": ["tue", "fri"],
                                            "value": "1.00", "kids": [claire]}, follow_redirects=False)
    assert "ok=" in r.headers["location"]
    c = service.list_chores(db_path=db)[0]
    assert c["days"] == ["tue", "fri"] and c["target_count"] == 2 and c["value_cents"] == 100 and c["kids"] == ["Claire"]

    client.post(f"/chores/chores/{c['id']}/update", data={
        "edit_schedule": "1", "edit_kids": "1", "frequency": "daily", "value": "2.00", "name": "Trash"})
    c = service.list_chores(db_path=db)[0]
    assert c["frequency"] == "daily" and c["days"] == [] and c["value_cents"] == 200 and c["kids"] == []

    assert client.get("/chores/rewards").status_code == 404  # simplified: no rewards page for now
    assert "Rewards" not in client.get("/chores/manage").text


def test_chores_write_errors_and_origin_guard(client, chores_svc):
    service, db, kid, chore = _seed_chart(chores_svc)
    r = client.post("/chores/payout", data={"kid_id": kid, "amount": "5"}, follow_redirects=False)
    assert "error=" in r.headers["location"] and "cannot+pay+out" in r.headers["location"]
    r = client.post("/chores/chores", data={"name": "x", "target": "abc"}, follow_redirects=False)
    assert "whole+number" in r.headers["location"]
    blocked = client.post("/chores/kids", data={"name": "Evil"}, headers={"Origin": "https://evil.example"},
                          follow_redirects=False)
    assert blocked.status_code == 403 and len(service.list_kids(db_path=db)) == 1
    

def test_chores_summary_tile(client, chores_svc):
    service, db, kid, chore = _seed_chart(chores_svc)
    tile = next(t for t in client.get("/api/summary").json()["tiles"] if t["title"] == "Chores")
    assert tile["headline"] == "0/1 done today"
    client.post(f"/kids/{kid}/chores/{chore}/done", headers={"HX-Request": "true"})
    tile = next(t for t in client.get("/api/summary").json()["tiles"] if t["title"] == "Chores")
    assert tile["headline"] == "1/1 done today" and "$0.50" in tile["sub"]


# ---- recipes: add / edit / delete (shared service: scripts/recipes.py) ----

def test_recipes_add_edit_delete_via_ui(client):
    assert client.get("/meals/new").status_code == 200
    r = client.post("/meals", data={
        "title": "Chili <b>", "meal_type": "dinner", "protein_type": "beef", "tags": "Crockpot, Spicy",
        "prep_time_min": "10", "cook_time_min": "50", "servings": "6",
        "ingredients": "1 lb beef\n2 cans beans\nsalt (pantry)", "steps": "Brown beef\nSimmer"},
        follow_redirects=False)
    assert r.status_code == 303
    rid = r.headers["location"].rsplit("/", 1)[1]
    page = client.get(f"/meals/{rid}").text
    assert "Chili &lt;b&gt;" in page and "Chili <b>" not in page and "(pantry)" in page
    recipe = client.get(f"/api/recipes/{rid}").json()
    assert recipe["total_time_min"] == 60 and recipe["tags"] == ["Crockpot", "Spicy"]
    assert [i["shopping"] for i in recipe["ingredients"]] == [True, True, False]

    edit = client.get(f"/meals/{rid}/edit").text
    assert "salt (pantry)" in edit and "Brown beef" in edit
    r = client.post(f"/meals/{rid}/update", data={
        "title": "Chili", "tags": "Crockpot", "prep_time_min": "5", "cook_time_min": "50",
        "ingredients": "1 lb beef", "steps": "Cook"}, follow_redirects=False)
    assert r.status_code == 303
    recipe = client.get(f"/api/recipes/{rid}").json()
    assert recipe["title"] == "Chili" and recipe["total_time_min"] == 55 and recipe["servings"] is None

    assert client.post(f"/meals/{rid}/delete", follow_redirects=False).status_code == 303
    assert client.get(f"/api/recipes/{rid}").status_code == 404


def test_recipes_write_errors_and_origin_guard(client):
    r = client.post("/meals", data={"title": " ", "ingredients": "x"})
    assert r.status_code == 400 and "title is required" in r.text
    r = client.post("/meals", data={"title": "T", "prep_time_min": "abc"})
    assert r.status_code == 400 and "whole number" in r.text and 'value="T"' in r.text
    assert client.post("/meals/nope/update", data={"title": "T"}).status_code == 404
    assert client.post("/meals/nope/delete").status_code == 404
    assert client.get("/meals/nope/edit").status_code == 404
    evil = {"Origin": "https://evil.example"}
    assert client.post("/meals", data={"title": "T"}, headers=evil).status_code == 403
    assert client.post("/meals/r1/update", data={"title": "T"}, headers=evil).status_code == 403
    assert client.post("/meals/r1/delete", headers=evil).status_code == 403
    assert client.get("/api/recipes/r1").json()["title"] == "Teriyaki Chicken"


def test_recipes_ui_and_service_share_data(client, state):
    """A recipe written through the agent CLI's service shows in the UI, and vice versa."""
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import recipes as service
    db = state / "agent_results.db"
    rid = service.add_recipe("Service Soup", ["water"], ["boil"], db_path=db)["id"]
    assert any(r["id"] == rid for r in client.get("/api/recipes").json()["recipes"])
    client.post(f"/meals/{rid}/update", data={"title": "Edited Soup", "ingredients": "water", "steps": "boil"})
    assert service.get_recipe(rid, db_path=db)["title"] == "Edited Soup"
    client.post(f"/meals/{rid}/delete")
    with pytest.raises(service.RecipeError):
        service.get_recipe(rid, db_path=db)


# ---- day rating (shared services: scripts/dayrating.py, scripts/insights.py) ----

def test_rating_ui_and_service_share_data(client, state):
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import dayrating as service
    r = client.post("/rating", data={"score": "8", "note": "good <b>day"}, headers={"Origin": "http://testserver"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert service.get_rating(db_path=state / "agent_results.db")["score"] == 8
    assert client.get("/api/rating").json()["status"]["today"]["note"] == "good <b>day"
    assert "<b>day" not in client.get("/rating").text
    r = client.post("/rating", data={"score": "11"}, headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert "between" in r.headers["location"]


# ---- Wellness (shared service: scripts/wellness.py; SVG charts: dashboard/charts.py) ----

def _seed_wellness(db):
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import wellness as service
    today = datetime.now().date()
    conn = service.connect(db)
    for i in range(1, 40):
        day = (today - timedelta(days=i)).isoformat()
        for metric, v in (("coros:sleep_hours", 6.5 + i % 3), ("coros:sleep_score", 60 + i % 30),
                          ("coros:hrv_ms", 40 + i % 7), ("coros:resting_hr", 50 + i % 4), ("coros:steps", 7000 + i * 10),
                          ("coros:training_load", 50 + i % 5), ("coros:bedtime_after_noon", 10.5 + (i % 3) / 4),
                          ("coros:wake_hour", 6.5), ("coros:deep_pct", 20), ("coros:rem_pct", 20),
                          ("coros:light_pct", 55)):
            conn.execute("INSERT OR REPLACE INTO insight_daily VALUES (?,?,?,?,?)",
                         (metric, day, v, "coros_health", datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()
    for i in range(1, 15):
        service.dayrating.set_rating(5 + i % 4, "note <i>x</i>" if i == 1 else None,
                                     rating_date=(today - timedelta(days=i)).isoformat(), db_path=db)
    return service


def test_wellness_ui_and_service_share_data(client, state):
    db = state / "agent_results.db"
    service = _seed_wellness(db)
    api = client.get("/api/wellness/trends?range=30").json()
    assert api["series"]["sleep_hours"] == service.trends(30, db_path=db)["series"]["sleep_hours"]
    today = client.get("/api/wellness").json()
    assert today["mood"]["shown"]["score"] == service.today_view(db_path=db)["mood"]["shown"]["score"]
    assert client.get("/api/wellness/scatter?x=sleep_hours&y=rating").json() == service.scatter(db_path=db)
    page = client.get("/wellness").text
    assert "Last night" in page and "<i>x</i>" not in page          # notes are escaped
    trends = client.get("/wellness/trends?range=30").text
    assert trends.count('class="wchart"') >= 10 and "data-tip='" in trends
    tile = next(t for t in client.get("/api/summary").json()["tiles"] if t["title"] == "Wellness")
    assert tile["href"] == "/wellness" and "sleep" in tile["sub"]


def test_wellness_bad_input(client):
    assert client.get("/api/wellness/trends?range=7").status_code == 400
    assert client.get("/api/wellness/scatter?x=nope").status_code == 400
    assert client.get("/api/wellness/drivers?target=mood").status_code == 400
    r = client.get("/wellness/trends?range=7")
    assert r.status_code == 200 and "range must be one of" in r.text


def test_wellness_charts_are_valid_svg_and_theme_driven():
    import re
    import xml.etree.ElementTree as ET
    from dashboard import charts
    dates = [f"2026-10-{d:02d}" for d in range(1, 11)]
    vals = {d: float(i) for i, d in enumerate(dates[:-1])}
    svgs = [
        charts.timeseries(dates, [
            {"kind": "zones", "zones": [(0, 3, "zone-ok", "ok")]},
            {"kind": "band", "low": vals, "high": {d: v + 2 for d, v in vals.items()}, "cls": "b", "label": "Band"},
            {"kind": "bars", "values": vals, "status": {dates[-1]: "pending"}, "cls": "s", "label": "V"},
            {"kind": "line", "values": vals, "cls": "l", "label": "L", "dots": True},
            {"kind": "range", "low": vals, "high": {d: v + 1 for d, v in vals.items()}, "label": "R",
             "fmt_low": str, "fmt_high": str},
            {"kind": "stack", "parts": [("A", vals, "a")]}, {"kind": "hline", "y": 4, "label": "T"},
            {"kind": "info", "values": vals, "label": "I"}], label="x"),
        charts.ring(50, zone="good", center="50%"), charts.ring(None),
        charts.spark([{"date": d, "value": vals.get(d)} for d in dates], band={"low": 1, "high": 3}),
        charts.heatmap([[{"date": d, "value": 1.0, "status": "final", "level": 3, "display": "1"} for d in dates[:7]]]),
        charts.scatter([{"date": d, "x": v, "y": v * 2} for d, v in vals.items()], fit={"slope": 2, "intercept": 0}),
        charts.category_bars([{"label": "Mon", "value": 3}, {"label": "Tue", "value": None}]),
    ]
    for svg in svgs:
        root = ET.fromstring(str(svg))
        assert root.tag == "svg"
        assert not re.search(r'(fill|stroke)="#', str(svg)), "colours must come from CSS"
    ts = str(svgs[0])
    assert ts.count('class="hit"') == 10 and "pending-mark" in ts and "Not synced yet" in ts
    tip = ET.fromstring(ts).find(".//{*}rect[@class='hit']").get("data-tip")
    assert json.loads(tip)["rows"][0] == ["Band", "0 – 2", "b"]


# ---- Journal + nightly check-in (shared service: scripts/journal.py) ----

def test_checkin_ui_and_service_share_data(client, state):
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import habits
    import journal as service
    db = state / "agent_results.db"
    hid = habits.create_habit("Stretch", "daily", 1, db_path=db)["id"]
    today = datetime.now().date().isoformat()
    r = client.post("/wellness/checkin", data={"date": today, "score": "7", "note": "fine <b>day", "q_alcohol": "yes",
                                               "q_thc": "no", "habit": hid},
                    headers={"Origin": "http://testserver", "HX-Request": "true"})
    assert r.status_code == 200 and "Saved." in r.text and "fine <b>day" not in r.text   # escaped
    p = service.pending(today, db_path=db)
    assert p["rating"]["score"] == 7 and p["habits_open"] == []
    assert {q["slug"]: q["answer"] for q in p["questions"]}["alcohol"] == "yes"
    assert client.get(f"/api/journal/checkin?date={today}").json() == p
    # service writes show up in the UI
    service.set_answer("late_meal", "yes", db_path=db)
    assert "late_meal" in p["unanswered"]
    assert "late_meal" not in client.get(f"/api/journal/checkin?date={today}").json()["unanswered"]
    hist = client.get("/api/journal/history?days=7").json()
    assert next(q for q in hist["questions"] if q["slug"] == "late_meal")["cells"][-1]["answer"] == "yes"
    assert 'id="checkin"' in client.get("/wellness").text
    # plain (no-htmx) post redirects back; errors come back as a message
    r = client.post("/wellness/checkin", data={"date": today, "score": "12", "next": "/wellness"},
                    headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/wellness?error=")
    r = client.post("/wellness/checkin", data={"date": today, "next": "https://evil.example"},
                    headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert r.headers["location"].startswith("/wellness/journal")


def test_journal_questions_via_ui_and_origin_guard(client, state):
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import journal as service
    db = state / "agent_results.db"
    ok = {"Origin": "http://testserver"}
    assert client.post("/journal/questions", data={"text": "Nap"}, headers=ok, follow_redirects=False).status_code == 303
    assert service.resolve_question("nap", db_path=db)["text"] == "Nap?"
    client.post("/journal/questions/nap/update", data={"active": "0"}, headers=ok, follow_redirects=False)
    assert service.resolve_question("nap", db_path=db)["active"] == 0
    bad = {"Origin": "https://evil.example"}
    for path, data in (("/wellness/checkin", {"score": "5"}), ("/journal/questions", {"text": "X"}),
                       ("/journal/questions/nap/update", {"active": "1"})):
        assert client.post(path, data=data, headers=bad).status_code == 403
    assert client.get("/api/journal/checkin?date=2099-01-01").status_code == 400


# ---- My Hub (shared service: scripts/myhub.py) ----

def test_hub_ui_and_service_share_data(client, state):
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import myhub as service
    service.set_snapshot("task", [{"title": "Renew <b>passport", "day": "2020-01-01", "priority": 1}],
                         db_path=state / "agent_results.db")
    api = client.get("/api/me").json()
    assert any("Renew <b>passport" in a["text"] for g in api["groups"] for a in g["items"])  # tasks live under Show all > Tasks
    page = client.get("/me")
    assert page.status_code == 200 and "My Hub" in page.text
    assert "passport" in page.text and "<b>passport" not in page.text  # escaped


def test_hub_dismiss_restore_and_new_event_still_shows(client, state):
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import myhub as service
    db = state / "agent_results.db"
    service.set_snapshot("task", [{"title": "Old task", "day": "2020-01-01", "priority": 1}], db_path=db)
    api = client.get("/api/me").json()
    task = api["groups"][0]["items"][0]
    hdr = {"Origin": "http://testserver"}
    r = client.post("/me/attention/dismiss", data={"key": task["key"]}, headers=hdr, follow_redirects=False)
    assert r.status_code == 303
    api = client.get("/api/me").json()
    assert not api["groups"] and [a["key"] for a in api["dismissed"]] == [task["key"]]
    assert "Dismissed (1)" in client.get("/me").text
    assert service.get_hub(db_path=db)["dismissed"][0]["key"] == task["key"]  # service agrees
    client.post("/me/attention/restore", data={"key": task["key"]}, headers=hdr)
    assert client.get("/api/me").json()["groups"]
    assert client.post("/me/attention/dismiss", data={"key": "x"},
                       headers={"Origin": "https://evil.example"}).status_code == 403
    assert service.restore(db_path=db) == {"restored": 0}
    try:
        service.dismiss("nope", db_path=db)
    except service.HubError as exc:
        assert exc.code == 1


def test_summary_and_hub_share_one_dismissal_store(client, state):
    """Dismissing on the Summary page is visible to the hub service (and so to notifications), and vice versa."""
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import myhub as service
    db = state / "agent_results.db"
    _seed_recent_failure(state)
    (item,) = _sched_items(client)
    client.post("/attention/dismiss", data={"key": item["key"]})
    assert item["key"] in service.dismissed_rows(db_path=db)
    service.dismiss_key("other:key", "x", db_path=db)
    client.post("/attention/restore", data={"key": item["key"]})
    assert item["key"] not in service.dismissed_rows(db_path=db)


def test_legacy_dismissal_file_is_imported_once(client, state):
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import myhub as service
    (state / "dashboard_dismissed.json").write_text('{"legacy:k": {"at": "2026-10-01T00:00:00+00:00"}}')
    client.get("/api/summary")
    from dashboard import dismissals
    dismissals.load()
    assert "legacy:k" in service.dismissed_rows(db_path=state / "agent_results.db")
    assert not (state / "dashboard_dismissed.json").exists()
