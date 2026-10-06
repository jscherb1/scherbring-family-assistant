"""Shared web plumbing: Jinja templates, filters, and the section/page navigation registry.

Adding a new section = a new router in routers/ plus one entry in SECTIONS below.
"""
from datetime import datetime
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

APP_NAME = "Scherbring Family Assistant"
HERE = Path(__file__).resolve().parent

SECTIONS = [
    {
        "key": "health",
        "label": "Orchestrator Health",
        "href": "/health",
        "pages": [
            {"key": "overview", "label": "Overview", "href": "/health"},
            {"key": "schedule", "label": "Scheduled Tasks", "href": "/health/schedule"},
            {"key": "restarts", "label": "Restart History", "href": "/health/restarts"},
        ],
    },
    {
        "key": "meals",
        "label": "Meal Planner",
        "href": "/meals",
        "pages": [
            {"key": "recipes", "label": "Recipes", "href": "/meals"},
        ],
    },
]

templates = Jinja2Templates(directory=str(HERE / "templates"))


def _to_datetime(value):
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt_dt(value, empty="never"):
    """'Oct 6, 8:34 AM'. Aware datetimes convert to server-local time; the naive
    timestamps in the scheduler DB are already local wall-clock and shown as-is."""
    dt = _to_datetime(value)
    if dt is None:
        return empty if not value else str(value)
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%b %-d, %-I:%M %p")


def fmt_duration(seconds):
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes}m"
    return f"{hours // 24}d {hours % 24}h"


def fmt_minutes(minutes):
    if not minutes:
        return "—"
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} hr {rest} min" if rest else f"{hours} hr"


def status_pill(status):
    return {"ok": "pill-ok", "failed": "pill-warn", "dispatch_failed": "pill-warn"}.get(
        status, "pill-muted")


def stars(rating):
    rating = max(0, min(5, int(rating or 0)))
    return "★" * rating + "☆" * (5 - rating)


templates.env.filters.update(
    fmt_dt=fmt_dt, fmt_duration=fmt_duration, fmt_minutes=fmt_minutes,
    status_pill=status_pill, stars=stars,
)
templates.env.globals["APP_NAME"] = APP_NAME


def render(request: Request, template: str, section: str, page: str,
           title: str, status_code: int = 200, **context):
    """Render a page inside base.html with navigation state."""
    current = next((s for s in SECTIONS if s["key"] == section), SECTIONS[0])
    return templates.TemplateResponse(
        request, template,
        {"sections": SECTIONS, "section": current, "page": page, "page_title": title, **context},
        status_code=status_code,
    )
