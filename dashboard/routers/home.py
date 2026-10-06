"""Home & Yard section: home maintenance, lawn & garden, weather alerts (read-only)."""
from fastapi import APIRouter, Request

from ..data import home
from ..web import render

router = APIRouter()


@router.get("/home", include_in_schema=False)
def maintenance(request: Request):
    return render(request, "home/maintenance.html", "home", "maintenance", "Home Maintenance",
                  data=home.get_maintenance())


@router.get("/home/lawn", include_in_schema=False)
def lawn(request: Request):
    return render(request, "home/lawn.html", "home", "lawn", "Lawn & Garden", data=home.get_lawn())


@router.get("/home/weather", include_in_schema=False)
def weather(request: Request):
    return render(request, "home/weather.html", "home", "weather", "Weather Alerts",
                  data=home.get_weather())


@router.get("/api/home/maintenance")
def api_maintenance():
    return home.get_maintenance()


@router.get("/api/home/lawn")
def api_lawn():
    return home.get_lawn()


@router.get("/api/home/weather")
def api_weather():
    return home.get_weather()
