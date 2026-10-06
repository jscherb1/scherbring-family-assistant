"""Activity section: what the assistant's agents have been doing (summaries only)."""
from fastapi import APIRouter, Request

from ..data import activity
from ..web import render

router = APIRouter()


@router.get("/activity", include_in_schema=False)
def feed(request: Request, agent: str = ""):
    return render(request, "activity/feed.html", "activity", "feed", "Agent Activity",
                  data=activity.get_activity(agent))


@router.get("/api/activity")
def api_activity(agent: str = ""):
    return activity.get_activity(agent)
