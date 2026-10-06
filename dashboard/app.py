"""Scherbring Family Assistant dashboard — read-only web app (FastAPI + Jinja + htmx).

Reads existing state from ../state/ (SQLite + JSON/JSONL/log files) and never writes
to any of it. Run from the repo root:

    .venv/bin/python -m uvicorn dashboard.app:app --host 127.0.0.1 --port 5151
"""
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .routers import activity, family, finance, health, home, meals, shopping_fitness
from .web import APP_NAME, HERE

app = FastAPI(
    title=APP_NAME,
    description="Read-only family assistant dashboard.",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    redoc_url=None,
)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
app.include_router(finance.router)
app.include_router(health.router)
app.include_router(activity.router)
app.include_router(family.router)
app.include_router(home.router)
app.include_router(meals.router)
app.include_router(shopping_fitness.router)


@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse("/health")
