"""Scherbring Family Assistant dashboard (FastAPI + Jinja + htmx).

Reads existing state from ../state/ (SQLite + JSON/JSONL/log files) and never writes to
it, with two exceptions: the landing page's one-shot "ask" box (routers/ask.py) queues a
message in state/inbox.db for the agent, and the habits section (routers/habits.py) logs
completions and edits habits through the shared service in scripts/habits.py, and the chores section (routers/chores.py)
and kid page (routers/kids.py) do the same through scripts/chores.py. Run from the repo root:

    .venv/bin/python -m uvicorn dashboard.app:app --host 127.0.0.1 --port 5151
"""
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from .routers import (activity, agent, ask, chores, dayrating, myhub, family, reauth, finance, habits, health, home, home_summary, kids, meals,
                      shopping_fitness, wellness)
from .web import APP_NAME, HERE

app = FastAPI(
    title=APP_NAME,
    description="Family assistant dashboard.",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    redoc_url=None,
)


@app.middleware("http")
async def no_stale_pages(request: Request, call_next):
    """Pages and static files revalidate every time (the CSS/JS URLs are also versioned)."""
    response = await call_next(request)
    if "cache-control" not in response.headers and not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# Chart-heavy pages (Wellness trends carry a tooltip per day per chart) shrink ~8x compressed.
app.add_middleware(GZipMiddleware, minimum_size=2048)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
app.include_router(agent.router)
app.include_router(finance.router)
app.include_router(health.router)
app.include_router(activity.router)
app.include_router(family.router)
app.include_router(home.router)
app.include_router(habits.router)
app.include_router(wellness.router)
app.include_router(dayrating.router)
app.include_router(myhub.router)
app.include_router(reauth.router)
app.include_router(chores.router)
app.include_router(kids.router)
app.include_router(meals.router)
app.include_router(shopping_fitness.router)


app.include_router(home_summary.router)
app.include_router(ask.router)

ICONS = HERE / "static" / "icons"
MANIFEST = {
    "name": APP_NAME,
    "short_name": "Scherbring",
    "description": "Family assistant dashboard",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#f5f5f7",
    "theme_color": "#0a74e8",
    "icons": [
        {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
        {"src": "/static/icons/icon-512-maskable.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    return JSONResponse(MANIFEST, media_type="application/manifest+json")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(ICONS / "favicon.ico", media_type="image/png")
