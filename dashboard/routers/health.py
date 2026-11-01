"""System tab: the status board (/health), scheduled tasks, logins & backups."""
from fastapi import APIRouter, Request

from ..data import health, ops, schedule, status
from ..web import render

router = APIRouter()


@router.get("/health", include_in_schema=False)
def overview(request: Request):
    return render(request, "health/index.html", "health", "overview", "System status", b=status.get_status_board())


@router.get("/api/system/status")
def api_status_board():
    return status.get_status_board()


@router.get("/health/schedule", include_in_schema=False)
def schedule_page(request: Request):
    data = schedule.list_tasks()
    return render(request, "health/schedule.html", "health", "schedule", "Scheduled Tasks",
                  data=data)


@router.get("/api/status")
def api_status():
    return health.get_status()


@router.get("/api/schedule")
def api_schedule():
    return schedule.list_tasks()


@router.get("/health/system", include_in_schema=False)
def system(request: Request):
    return render(request, "health/system.html", "health", "system", "Logins & Backups",
                  data=ops.get_system())


@router.get("/api/system")
def api_system():
    return ops.get_system()
