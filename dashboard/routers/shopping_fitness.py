"""Hy-Vee and Fitness sections (read-only)."""
from fastapi import APIRouter, Request

from ..data import shopping_fitness as data
from ..web import render

router = APIRouter()


@router.get("/hyvee", include_in_schema=False)
def hyvee(request: Request):
    return render(request, "hyvee/overview.html", "hyvee", "overview", "Hy-Vee Cart Builder",
                  data=data.get_hyvee())


@router.get("/fitness", include_in_schema=False)
def fitness(request: Request):
    return render(request, "fitness/overview.html", "fitness", "overview", "Fitness Plan",
                  data=data.get_fitness())


@router.get("/api/hyvee")
def api_hyvee():
    return data.get_hyvee()


@router.get("/api/fitness")
def api_fitness():
    return data.get_fitness()
