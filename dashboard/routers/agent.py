"""Agent section: bridge/worker liveness, live event stream, usage."""
from fastapi import APIRouter, Request

from ..data import agent
from ..web import render

router = APIRouter()


@router.get("/agent", include_in_schema=False)
def live(request: Request):
    return render(request, "agent/index.html", "agent", "live", "Agent (Telegram bridge)",
                  status=agent.get_agent_status(), stream=agent.get_stream(), fastlane=agent.get_fastlane())


@router.get("/agent/panel", include_in_schema=False)
def panel(request: Request):
    """htmx partial, refreshed every few seconds."""
    return render(request, "agent/_panel.html", "agent", "live", "Agent",
                  status=agent.get_agent_status(), stream=agent.get_stream())


@router.get("/agent/usage", include_in_schema=False)
def usage_page(request: Request):
    return render(request, "agent/usage.html", "agent", "usage", "Agent usage", usage=agent.get_usage())


@router.get("/api/agent")
def api_agent():
    return {"status": agent.get_agent_status(), "stream": agent.get_stream(), "usage": agent.get_usage(), "fastlane": agent.get_fastlane()}
