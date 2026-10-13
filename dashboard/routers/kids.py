"""Kid-facing chore page: one shared screen, tap a chore to mark it done.

Reads and writes go through the shared service in scripts/chores.py (via data/chores.py), the
same code the `chores` subagent's CLI uses. Kid routes can only complete or undo *today's*
chores (the service defaults); backfill, payouts and edits live in the adult /chores section.
Trust mode: a tap counts immediately. Writes use the same-origin guard; access control is the
network (Tailscale-only bind), per the dashboard's README.
"""
import json

from fastapi import APIRouter, Request
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from ..data import chores as data, common
from ..web import templates
from .ask import _same_origin

router = APIRouter()
service = data.service


def _blocked(request: Request):
    if not _same_origin(request):
        return JSONResponse({"error": "Blocked: the request didn't come from this site."}, status_code=403)
    return None


def _column(request: Request, kid_id: str, error: str | None = None, celebrate: dict | None = None):
    """Re-render one kid's column. 200 even on error so htmx swaps it and shows the message inline."""
    resp = templates.TemplateResponse(request, "kids/_column.html", {"v": data.get_kid(kid_id), "error": error})
    if celebrate:
        # ensure_ascii keeps the header latin-1 safe (emoji become \u escapes htmx decodes).
        resp.headers["HX-Trigger"] = json.dumps({"chore-celebrate": celebrate})
    return resp


@router.get("/kids", include_in_schema=False)
def kids_page(request: Request):
    return templates.TemplateResponse(request, "kids/index.html", {"data": data.get_kids_page(),
                                                                    "page_title": "Chore Chart"})


@router.get("/kids/{kid_name}", include_in_schema=False)
def kid_page(request: Request, kid_name: str):
    """One kid's own chart (for that kid's tablet): same template and data as /kids, filtered to one kid.
    Exact name match only (case-insensitive) so '/kids/r' doesn't resolve to Ruth."""
    page = data.get_kids_page()
    mine = [v for v in page.get("kids", []) if v["kid"]["name"].lower() == kid_name.lower()]
    if not mine:
        raise HTTPException(status_code=404, detail="No such kid")
    name = mine[0]["kid"]["name"]
    return templates.TemplateResponse(request, "kids/index.html", {
        "data": {"kids": mine, "message": page.get("message")},
        "page_title": f"{name}'s Chores", "solo": True})


@router.post("/kids/{kid_id}/chores/{chore_id}/done", include_in_schema=False)
def done(request: Request, kid_id: str, chore_id: str):
    if (resp := _blocked(request)):
        return resp
    try:
        r = service.complete(kid_id, chore_id, db_path=common.db_path())
    except service.ChoreError as exc:
        return _column(request, kid_id, exc.message)
    return _column(request, kid_id, celebrate={
        "kid": r["kid"], "earned": r["earned"] if r["earned_cents"] else "",
        "badges": r["new_badges"], "all_done": r["all_done"],
    })


@router.post("/kids/{kid_id}/chores/{chore_id}/undo", include_in_schema=False)
def undo(request: Request, kid_id: str, chore_id: str):
    if (resp := _blocked(request)):
        return resp
    try:
        service.undo(kid_id, chore_id, db_path=common.db_path())
    except service.ChoreError as exc:
        return _column(request, kid_id, exc.message)
    return _column(request, kid_id)
