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
            {"key": "system", "label": "Logins & Backups", "href": "/health/system"},
        ],
    },
    {
        "key": "home",
        "label": "Home & Yard",
        "href": "/home",
        "pages": [
            {"key": "maintenance", "label": "Maintenance", "href": "/home"},
            {"key": "lawn", "label": "Lawn & Garden", "href": "/home/lawn"},
            {"key": "weather", "label": "Weather Alerts", "href": "/home/weather"},
        ],
    },
    {
        "key": "family",
        "label": "People & Family",
        "href": "/family",
        "pages": [
            {"key": "profile", "label": "Profile", "href": "/family"},
            {"key": "memories", "label": "Kids Memories", "href": "/family/memories"},
        ],
    },
    {
        "key": "finance",
        "label": "Finance",
        "href": "/finance",
        "pages": [
            {"key": "tagging", "label": "Tagging", "href": "/finance"},
            {"key": "reports", "label": "Reports & Planning", "href": "/finance/reports"},
        ],
    },
    {
        "key": "hyvee",
        "label": "Hy-Vee",
        "href": "/hyvee",
        "pages": [{"key": "overview", "label": "Overview", "href": "/hyvee"}],
    },
    {
        "key": "fitness",
        "label": "Fitness",
        "href": "/fitness",
        "pages": [{"key": "overview", "label": "Overview", "href": "/fitness"}],
    },
    {
        "key": "meals",
        "label": "Meal Planner",
        "href": "/meals",
        "pages": [
            {"key": "recipes", "label": "Recipes", "href": "/meals"},
            {"key": "plan", "label": "Menu & Insights", "href": "/meals/plan"},
        ],
    },
    {
        "key": "activity",
        "label": "Activity",
        "href": "/activity",
        "pages": [{"key": "feed", "label": "Agent Activity", "href": "/activity"}],
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
    return {
        "ok": "pill-ok", "done": "pill-ok", "synced": "pill-ok", "created": "pill-ok",
        "failed": "pill-warn", "pending": "pill-muted", "dispatch_failed": "pill-warn", "overdue": "pill-warn",
        "missed": "pill-warn", "manual_needed": "pill-warn", "expired": "pill-warn",
        "active": "pill-warn", "warn": "pill-warn",
    }.get(status, "pill-muted")


def fmt_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def stars(rating):
    rating = max(0, min(5, int(rating or 0)))
    return "★" * rating + "☆" * (5 - rating)


templates.env.filters.update(
    fmt_dt=fmt_dt, fmt_duration=fmt_duration, fmt_minutes=fmt_minutes,
    status_pill=status_pill, stars=stars, fmt_bytes=fmt_bytes,
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
