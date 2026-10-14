"""Day rating section: rate today (or a past day) 1-10 with a note, and the insights page.

Every read and write goes through the shared services in scripts/dayrating.py and scripts/insights.py
(via data/dayrating.py), the same code the `dayrating` subagent's CLI uses. Writes are guarded by the
same-origin check the ask box uses.
"""
from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..data import common, dayrating as data
from ..web import render
from .ask import _same_origin

router = APIRouter()
service = data.service


@router.get("/rating", include_in_schema=False)
def rating_page(request: Request, error: str = ""):
    return render(request, "dayrating/index.html", "dayrating", "today", "Day rating",
                  data=data.get_overview(), error=error)


@router.post("/rating", include_in_schema=False)
def rate(request: Request, score: str = Form(""), note: str = Form(""), rating_date: str = Form("")):
    if not _same_origin(request):
        return JSONResponse({"error": "Blocked: the request didn't come from this site."}, status_code=403)
    try:
        service.set_rating(score, note or None, rating_date=rating_date or None, db_path=common.db_path())
    except service.RatingError as exc:
        return RedirectResponse(f"/rating?error={exc.message.replace(' ', '+')}", status_code=303)
    return RedirectResponse("/rating", status_code=303)


@router.get("/rating/insights", include_in_schema=False)
def insights_page(request: Request):
    return render(request, "dayrating/insights.html", "dayrating", "insights", "Day insights",
                  data=data.get_insights())


@router.get("/api/rating")
def api_rating(days: int = 60):
    return data.get_overview(days)


@router.get("/api/rating/insights")
def api_insights(days: int = 90):
    return data.get_insights(days)
