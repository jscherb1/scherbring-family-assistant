"""Orchestrator Health section: overview, scheduled tasks, restart history."""
from fastapi import APIRouter, Request

from ..data import health, restarts, schedule
from ..web import render

router = APIRouter()


@router.get("/health", include_in_schema=False)
def overview(request: Request):
    return render(request, "health/index.html", "health", "overview", "Orchestrator Health")


@router.get("/health/schedule", include_in_schema=False)
def schedule_page(request: Request):
    data = schedule.list_tasks()
    return render(request, "health/schedule.html", "health", "schedule", "Scheduled Tasks",
                  data=data)


@router.get("/health/restarts", include_in_schema=False)
def restarts_page(request: Request):
    data = restarts.get_restart_history()
    return render(request, "health/restarts.html", "health", "restarts", "Restart History",
                  data=data)


@router.get("/api/status")
def api_status():
    return health.get_status()


@router.get("/api/schedule")
def api_schedule():
    return schedule.list_tasks()


@router.get("/api/restarts")
def api_restarts():
    return restarts.get_restart_history()
