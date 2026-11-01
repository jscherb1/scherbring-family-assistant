#!/usr/bin/env python3
"""Todoist API client (stdlib only) for scripts that need Todoist without an agent.

Uses the current Todoist API v1 (https://developer.todoist.com/api/v1); REST v2 and Sync v9 are deprecated.
The token is read through paths.get_env("TODOIST_API_TOKEN") and is never printed or logged.

Errors are TodoistError with a short `code` ("no_token", "auth", "rate_limited", "http", "network", "bad_response",
"not_found") and a CLI-style exit `exit_code`.

CLI (read-only except `add`):
    python scripts/todoist_api.py projects
    python scripts/todoist_api.py tasks [--project NAME]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import paths  # noqa: E402

BASE = "https://api.todoist.com/api/v1"
API_VERSION = "v1"
TIMEOUT = 8
PAGE_LIMIT = 200
MAX_PAGES = 50


class TodoistError(Exception):
    def __init__(self, message: str, code: str = "http", exit_code: int = 1):
        super().__init__(message)
        self.message = message
        self.code = code
        self.exit_code = exit_code


def _token() -> str:
    tok = paths.get_env("TODOIST_API_TOKEN")
    if not tok:
        raise TodoistError("Todoist isn't configured (TODOIST_API_TOKEN).", "no_token")
    return tok


def _open(path: str, body=None, method: str | None = None):
    """One HTTP call; returns parsed JSON. Never includes the token in an error message."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method or ("POST" if data is not None else "GET"),
                                 headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310
            payload = resp.read()
            return json.loads(payload) if payload else None
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise TodoistError("Todoist rejected the token (HTTP %d); it may be revoked or expired." % exc.code,
                               "auth") from None
        if exc.code == 429:
            raise TodoistError("Todoist rate limit hit (HTTP 429); try again later.", "rate_limited") from None
        if exc.code == 404:
            raise TodoistError(f"Todoist returned HTTP 404 for {path.split('?')[0]}", "not_found") from None
        raise TodoistError(f"Todoist returned HTTP {exc.code} for {path.split('?')[0]}", "http") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TodoistError(f"Can't reach Todoist ({getattr(exc, 'reason', exc)})", "network") from None
    except ValueError:
        raise TodoistError("Todoist sent a response that isn't JSON.", "bad_response") from None


def _paged(path: str, params: dict | None = None) -> list[dict]:
    """Follow next_cursor until exhausted. Accepts a bare list too (older shape)."""
    out: list[dict] = []
    cursor = None
    for _ in range(MAX_PAGES):
        q = dict(params or {})
        q["limit"] = PAGE_LIMIT
        if cursor:
            q["cursor"] = cursor
        res = _open(f"{path}?{urllib.parse.urlencode(q)}")
        if isinstance(res, list):
            return out + res
        if not isinstance(res, dict) or not isinstance(res.get("results"), list):
            raise TodoistError("Todoist sent an unexpected response shape.", "bad_response")
        out.extend(res["results"])
        cursor = res.get("next_cursor")
        if not cursor:
            return out
    raise TodoistError("Todoist pagination did not finish.", "bad_response")


def list_projects() -> list[dict]:
    return _paged("/projects")


def list_open_tasks(project_id: str | None = None) -> list[dict]:
    """Open (not completed) tasks, all pages; optionally one project. The API only lists open tasks."""
    return _paged("/tasks", {"project_id": project_id} if project_id else None)


def resolve_project(name: str, projects: list[dict] | None = None) -> dict:
    """The project named `name` (case-insensitive, trimmed). TodoistError(not_found) if absent or ambiguous."""
    want = (name or "").strip().lower()
    found = [p for p in (projects if projects is not None else list_projects())
             if str(p.get("name", "")).strip().lower() == want]
    if not found:
        raise TodoistError(f"No Todoist project named {name!r}.", "not_found", 1)
    if len(found) > 1:
        raise TodoistError(f"More than one Todoist project is named {name!r}.", "not_found", 2)
    return found[0]


def is_on_list(content: str, tasks: list[dict]) -> bool:
    """True when an open task with the same text (case-insensitive, whitespace-collapsed) is in `tasks`."""
    norm = lambda s: " ".join(str(s or "").split()).lower()  # noqa: E731
    want = norm(content)
    return bool(want) and any(norm(t.get("content")) == want for t in tasks)


def task_on_project(content: str, project: str) -> bool:
    """Is this open task already on the named project? One read; for the fast lane's dedupe."""
    proj = resolve_project(project)
    return is_on_list(content, list_open_tasks(proj["id"]))


def add_task(content: str, project: str | None = None, *, due_string: str | None = None,
             priority: int | None = None, skip_if_present: bool = True) -> dict:
    """Create a task on a project resolved by name. By default an existing open task of the same text on that
    project is returned as {"skipped": True, ...} instead of adding a duplicate (pass skip_if_present=False to
    force one). priority is Todoist's API value (4 = p1 urgent, 1 = p4 normal), 1-4 only. The result names the
    project as Todoist spells it."""
    content = (content or "").strip()
    if not content:
        raise TodoistError("A task needs some text.", "bad_input", 2)
    if priority is not None and (isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 4):
        raise TodoistError(f"priority must be 1-4, got {priority!r}.", "bad_input", 2)
    body: dict = {"content": content}
    pname = None
    if project:
        proj = resolve_project(project)
        pname = proj["name"]
        body["project_id"] = proj["id"]
        if skip_if_present and is_on_list(content, list_open_tasks(proj["id"])):
            return {"skipped": True, "content": content, "project": pname}
    if due_string:
        body["due_string"] = due_string
    if priority is not None:
        body["priority"] = priority
    res = _open("/tasks", body)
    return {"skipped": False, "id": (res or {}).get("id"), "content": content, "project": pname}


def due_day(task: dict, tz=None) -> str | None:
    """YYYY-MM-DD the task is due, or None. Timed due values with a UTC marker are converted to local time."""
    due = task.get("due") or {}
    d = due.get("date")
    if not d:
        return None
    if len(d) > 10:
        from datetime import datetime, timezone
        try:
            dt = datetime.fromisoformat(d.replace("Z", "+00:00"))
            if dt.tzinfo is not None:  # any explicit offset (Z, +00:00, -05:00): convert to local
                return dt.astimezone(tz or timezone.utc).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return d[:10]  # a plain date, or a floating time that is already local


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("projects")
    t = sub.add_parser("tasks")
    t.add_argument("--project")
    args = p.parse_args(argv)
    try:
        if args.cmd == "projects":
            print(json.dumps([{"id": x.get("id"), "name": x.get("name")} for x in list_projects()], indent=2))
        else:
            pid = resolve_project(args.project)["id"] if args.project else None
            tasks = list_open_tasks(pid)
            print(json.dumps({"count": len(tasks), "tasks": [{"content": x.get("content"), "due": due_day(x)}
                                                              for x in tasks]}, indent=2))
        return 0
    except TodoistError as exc:
        print(json.dumps({"error": exc.message, "code": exc.code}), file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main())
