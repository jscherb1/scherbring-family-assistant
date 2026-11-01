"""Todoist client: pagination, typed errors, project resolution, dedupe. All HTTP is stubbed."""
import io
import json
import urllib.error

import pytest

import paths
import todoist_api as ta


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setattr(paths, "get_env", lambda key, extra=(): "test-token" if key == "TODOIST_API_TOKEN" else None)


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _urlopen(responses, seen=None):
    it = iter(responses)

    def fake(req, timeout=None):
        if seen is not None:
            seen.append((req.full_url, req.get_method(), req.data, timeout))
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return FakeResp(json.dumps(r).encode())
    return fake


def test_uses_api_v1_and_paginates(monkeypatch):
    seen = []
    pages = [{"results": [{"id": "1", "content": "a"}], "next_cursor": "c2"},
             {"results": [{"id": "2", "content": "b"}], "next_cursor": None}]
    monkeypatch.setattr(ta.urllib.request, "urlopen", _urlopen(pages, seen))
    tasks = ta.list_open_tasks()
    assert [t["id"] for t in tasks] == ["1", "2"]
    assert seen[0][0].startswith("https://api.todoist.com/api/v1/tasks?")
    assert "cursor=c2" in seen[1][0]
    assert seen[0][3] == ta.TIMEOUT == 8


def test_bare_list_shape_accepted(monkeypatch):
    monkeypatch.setattr(ta.urllib.request, "urlopen", _urlopen([[{"id": "p", "name": "Home"}]]))
    assert ta.list_projects()[0]["name"] == "Home"


@pytest.mark.parametrize("code,kind", [(401, "auth"), (403, "auth"), (429, "rate_limited"), (500, "http")])
def test_http_errors_are_typed(monkeypatch, code, kind):
    err = urllib.error.HTTPError("https://x", code, "m", {}, None)
    monkeypatch.setattr(ta.urllib.request, "urlopen", _urlopen([err]))
    with pytest.raises(ta.TodoistError) as e:
        ta.list_projects()
    assert e.value.code == kind
    assert "test-token" not in e.value.message


def test_network_and_timeout_errors(monkeypatch):
    monkeypatch.setattr(ta.urllib.request, "urlopen", _urlopen([urllib.error.URLError("boom")]))
    with pytest.raises(ta.TodoistError) as e:
        ta.list_projects()
    assert e.value.code == "network"
    monkeypatch.setattr(ta.urllib.request, "urlopen", _urlopen([TimeoutError()]))
    with pytest.raises(ta.TodoistError) as e:
        ta.list_projects()
    assert e.value.code == "network"


def test_missing_token(monkeypatch):
    monkeypatch.setattr(paths, "get_env", lambda *a, **k: None)
    with pytest.raises(ta.TodoistError) as e:
        ta.list_projects()
    assert e.value.code == "no_token"


def test_resolve_project_case_insensitive():
    projects = [{"id": "1", "name": "Shopping"}, {"id": "2", "name": "Home"}]
    assert ta.resolve_project(" shopping ", projects)["id"] == "1"
    with pytest.raises(ta.TodoistError) as e:
        ta.resolve_project("nope", projects)
    assert e.value.code == "not_found"


def test_is_on_list_case_and_whitespace():
    tasks = [{"content": "Whole  Milk"}]
    assert ta.is_on_list("whole milk", tasks)
    assert ta.is_on_list("  WHOLE MILK ", tasks)
    assert not ta.is_on_list("milk", tasks)
    assert not ta.is_on_list("", tasks)


def test_add_task_resolves_project_and_skips_duplicate(monkeypatch):
    seen = []
    responses = [
        {"results": [{"id": "9", "name": "Shopping"}], "next_cursor": None},       # projects
        {"results": [{"id": "t", "content": "Eggs"}], "next_cursor": None},        # open tasks on it
    ]
    monkeypatch.setattr(ta.urllib.request, "urlopen", _urlopen(responses, seen))
    res = ta.add_task("eggs", "shopping", skip_if_present=True)
    assert res["skipped"] is True
    assert all(s[1] == "GET" for s in seen)  # nothing was created

    seen.clear()
    responses = [{"results": [{"id": "9", "name": "Shopping"}], "next_cursor": None},
                 {"results": [], "next_cursor": None}, {"id": "new1"}]
    monkeypatch.setattr(ta.urllib.request, "urlopen", _urlopen(responses, seen))
    res = ta.add_task("Milk", "shopping")          # dedupe check is the default; name comes back as Todoist spells it
    assert res == {"skipped": False, "id": "new1", "content": "Milk", "project": "Shopping"}
    assert seen[2][1] == "POST" and json.loads(seen[2][2]) == {"content": "Milk", "project_id": "9"}


@pytest.mark.parametrize("bad", [0, 5, "4", 2.5, True])
def test_add_task_rejects_bad_priority_before_any_call(monkeypatch, bad):
    monkeypatch.setattr(ta.urllib.request, "urlopen", _urlopen([]))   # any HTTP call would raise StopIteration
    with pytest.raises(ta.TodoistError) as e:
        ta.add_task("x", "Shopping", priority=bad)
    assert e.value.code == "bad_input" and e.value.exit_code == 2


def test_due_day_converts_utc_to_local():
    import ha
    assert ta.due_day({"due": {"date": "2026-11-03"}}) == "2026-11-03"
    assert ta.due_day({"due": {"date": "2026-11-03T10:00:00"}}) == "2026-11-03"
    # 02:30Z is the evening before in Chicago
    assert ta.due_day({"due": {"date": "2026-11-04T02:30:00Z"}}, ha.local_tz()) == "2026-11-03"
    assert ta.due_day({"due": None}) is None
    tz = ha.local_tz()
    assert ta.due_day({"due": {"date": "2026-11-04T02:30:00+00:00"}}, tz) == "2026-11-03"
    assert ta.due_day({"due": {"date": "2026-11-03T21:00:00-05:00"}}, tz) == "2026-11-03"   # 20:00 Chicago
    assert ta.due_day({"due": {"date": "2026-11-04T01:00:00+05:30"}}, tz) == "2026-11-03"   # offsets other than Z/UTC
