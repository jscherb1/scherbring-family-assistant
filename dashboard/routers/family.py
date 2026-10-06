"""People & Family section: personal profile and kids memories (summaries only)."""
from fastapi import APIRouter, Request

from ..data import family
from ..web import render

router = APIRouter()


@router.get("/family", include_in_schema=False)
def profile(request: Request):
    return render(request, "family/profile.html", "family", "profile", "Family Profile",
                  data=family.get_profile())


@router.get("/family/memories", include_in_schema=False)
def memories(request: Request):
    return render(request, "family/memories.html", "family", "memories", "Kids Memories",
                  data=family.get_memories())


@router.get("/api/family/profile")
def api_profile():
    return family.get_profile()


@router.get("/api/family/memories")
def api_memories():
    return family.get_memories()
