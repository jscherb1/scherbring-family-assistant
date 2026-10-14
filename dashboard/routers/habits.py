"""Habits section: list, create, mark done, undo, pause.

Every read and write goes through the shared service in scripts/habits.py (via
data/habits.py), the same code the `habits` subagent's CLI uses. This router only does web
plumbing. Writes are guarded by the same-origin check the ask box uses; access control is
the network (Tailscale-only bind).
"""
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..data import common, habits as data
from ..web import render
from .ask import _same_origin

router = APIRouter()
service = data.service


def _blocked(request: Request):
    if not _same_origin(request):
        return JSONResponse({"error": "Blocked: the request didn't come from this site."}, status_code=403)
    return None


def _done(request: Request, habit_id: str | None, error: str | None = None):
    """After a write: htmx swaps just that habit's card; a plain form post redirects to the page."""
    if request.headers.get("hx-request") and habit_id:
        # 200 even on error: htmx only swaps 2xx, and the card shows the message inline.
        h = data.get_one(habit_id)
        if not h.get("error"):
            h["calendar"] = data.get_calendar(habit_id)
        return render(request, "habits/_card.html", "habits", "habits", "Habits", h=h, error=error)
    suffix = f"?error={error.replace(' ', '+')}" if error else ""
    return RedirectResponse(f"/habits{suffix}", status_code=303)


@router.get("/habits", include_in_schema=False)
def habits_page(request: Request, owner: str = "", error: str = ""):
    return render(request, "habits/index.html", "habits", "habits", "Habits",
                  data=data.get_habits(owner), error=error)


@router.get("/habits/analysis", include_in_schema=False)
def analysis_page(request: Request):
    return render(request, "habits/analysis.html", "habits", "analysis", "Habit analysis",
                  data=data.get_analysis())


@router.post("/habits", include_in_schema=False)
def create(request: Request, name: str = Form(""), frequency: str = Form("daily"), target: str = Form(""),
           owner: str = Form(""), description: str = Form("")):
    if (resp := _blocked(request)):
        return resp
    try:
        target_count = int(target) if target.strip() else None
        service.create_habit(name, frequency, target_count, owner=owner or None,
                             description=description or None, db_path=common.db_path())
    except ValueError:
        return _done(request, None, "Target must be a whole number")
    except service.HabitError as exc:
        return _done(request, None, exc.message)
    return RedirectResponse("/habits", status_code=303)


@router.post("/habits/{habit_id}/log", include_in_schema=False)
def log_done(request: Request, habit_id: str, log_date: str = Form(""), note: str = Form("")):
    if (resp := _blocked(request)):
        return resp
    try:
        service.log_completion(habit_id, log_date=log_date or None, note=note or None, db_path=common.db_path())
    except service.HabitError as exc:
        return _done(request, habit_id, exc.message)
    return _done(request, habit_id)


@router.post("/habits/{habit_id}/undo", include_in_schema=False)
def undo(request: Request, habit_id: str):
    if (resp := _blocked(request)):
        return resp
    try:
        service.undo_last(habit_id, db_path=common.db_path())
    except service.HabitError as exc:
        return _done(request, habit_id, exc.message)
    return _done(request, habit_id)


@router.post("/habits/{habit_id}/update", include_in_schema=False)
def update(request: Request, habit_id: str, active: str = Form(""), remind: str = Form("")):
    """Pause/resume and the nudge on/off toggle (the fields the cards expose)."""
    if (resp := _blocked(request)):
        return resp
    changes = {}
    if active in ("0", "1"):
        changes["active"] = active == "1"
    if remind in ("0", "1"):
        changes["remind"] = remind == "1"
    try:
        service.update_habit(habit_id, db_path=common.db_path(), **changes)
    except service.HabitError as exc:
        return _done(request, habit_id, exc.message)
    return _done(request, habit_id)


@router.get("/api/habits")
def api_habits(owner: str = "", include_paused: bool = True):
    return data.get_habits(owner, include_paused)


@router.get("/api/habits/analysis")
def api_analysis(weeks: int = 12):
    return data.get_analysis(weeks)




@router.get("/api/habits/{habit_id}")
def api_habit(habit_id: str):
    status = data.get_one(habit_id)
    if status.get("error", "").startswith("no habit"):
        raise HTTPException(404, status["error"])
    return status


@router.get("/api/habits/{habit_id}/calendar")
def api_calendar(habit_id: str, months: int = 2):
    cal = data.get_calendar(habit_id, months)
    if cal.get("error", "").startswith("no habit"):
        raise HTTPException(404, cal["error"])
    return cal
