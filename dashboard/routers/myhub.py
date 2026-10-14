"""My Hub: one visual page for everything that matters to the person.

Read-only. The page and its /api/me twin render the same service output (scripts/myhub.py).
"""
from fastapi import APIRouter, Request

from ..data import myhub as data
from ..web import render

router = APIRouter()


@router.get("/me", include_in_schema=False)
def hub_page(request: Request):
    return render(request, "myhub/index.html", "myhub", "hub", "My Hub", data=data.get_hub())


@router.get("/api/me")
def hub_api():
    return data.get_hub()
