"""Shared web plumbing: Jinja templates, filters, and the section/page navigation registry.

Adding a new section = a new router in routers/ plus one entry in SECTIONS below.
"""
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import TemplateNotFound

APP_NAME = "Scherbring Family Assistant"
HERE = Path(__file__).resolve().parent

# Navigation: six tabs (System sits behind the gear icon). Each page names the (section, page) pair its
# router passes to render() in "src", so routers keep their own keys while the nav is regrouped here.
# "group" puts a small heading over a run of pages in the sub-nav.
SECTIONS = [
    {
        "key": "today", "label": "Today", "icon": "today", "href": "/",
        "pages": [
            {"label": "Today", "href": "/", "src": "today:today"},
        ],
    },
    {
        "key": "me", "label": "Me", "icon": "me", "href": "/wellness",
        "pages": [
            {"group": "Wellness", "label": "Today", "href": "/wellness", "src": "wellness:today"},
            {"label": "Trends", "href": "/wellness/trends", "src": "wellness:trends"},
            {"label": "Drivers", "href": "/wellness/drivers", "src": "wellness:drivers"},
            {"group": "Log", "label": "Check-in", "href": "/checkin", "src": "checkin:today"},
            {"label": "Journal", "href": "/wellness/journal", "src": "wellness:journal"},
            {"label": "Insights", "href": "/rating/insights", "src": "dayrating:insights"},
            {"group": "Habits", "label": "Streaks", "href": "/habits", "src": "habits:habits"},
            {"label": "Analysis", "href": "/habits/analysis", "src": "habits:analysis"},
            {"group": "Training", "label": "Fitness", "href": "/fitness", "src": "fitness:overview"},
        ],
    },
    {
        "key": "family", "label": "Family", "icon": "family", "href": "/chores",
        "pages": [
            {"group": "Chores", "label": "Today", "href": "/chores", "src": "chores:overview"},
            {"label": "Chores & kids", "href": "/chores/manage", "src": "chores:manage"},
            {"label": "Money", "href": "/chores/ledger", "src": "chores:ledger"},
            {"label": "Kid chart ↗", "href": "/kids", "src": None},
            {"group": "People", "label": "Profile", "href": "/family", "src": "family:profile"},
            {"label": "Kids memories", "href": "/family/memories", "src": "family:memories"},
        ],
    },
    {
        "key": "house", "label": "Home", "icon": "home", "href": "/meals/plan",
        "pages": [
            {"group": "Food", "label": "Menu", "href": "/meals/plan", "src": "meals:plan"},
            {"label": "Recipes", "href": "/meals", "src": "meals:recipes"},
            {"label": "Groceries", "href": "/hyvee", "src": "hyvee:overview"},
            {"group": "House & yard", "label": "Maintenance", "href": "/home", "src": "home:maintenance"},
            {"label": "Lawn & garden", "href": "/home/lawn", "src": "home:lawn"},
            {"label": "Weather alerts", "href": "/home/weather", "src": "home:weather"},
        ],
    },
    {
        "key": "money", "label": "Money", "icon": "money", "href": "/finance",
        "pages": [
            {"label": "Tagging", "href": "/finance", "src": "finance:tagging"},
            {"label": "Reports & planning", "href": "/finance/reports", "src": "finance:reports"},
        ],
    },
    {
        "key": "system", "label": "System", "icon": "system", "href": "/health", "utility": True,
        "pages": [
            {"group": "Health", "label": "Status", "href": "/health", "src": "health:overview"},
            {"label": "Scheduled tasks", "href": "/health/schedule", "src": "health:schedule"},
            {"label": "Restarts", "href": "/health/restarts", "src": "health:restarts"},
            {"label": "Logins & backups", "href": "/health/system", "src": "health:system"},
            {"label": "Reconnect", "href": "/reconnect", "src": "health:reconnect"},
            {"group": "Agent", "label": "Live", "href": "/agent", "src": "agent:live"},
            {"label": "Usage", "href": "/agent/usage", "src": "agent:usage"},
            {"label": "Activity", "href": "/activity", "src": "activity:feed"},
            {"group": "Old pages", "label": "Archive", "href": "/archive", "src": "system:archive"},
        ],
    },
]

# The pre-redesign navigation, used only by the archived pages under /archive (see ARCHIVE below).
LEGACY_SECTIONS = [
    {
        "key": "summary",
        "label": "Summary",
        "href": "/",
        "pages": [{"key": "overview", "label": "Overview", "href": "/"}],
    },
    {
        "key": "myhub",
        "label": "My Hub",
        "href": "/me",
        "pages": [{"key": "hub", "label": "Hub", "href": "/me"}],
    },
    {
        "key": "health",
        "label": "Orchestrator Health",
        "href": "/health",
        "pages": [
            {"key": "overview", "label": "Overview", "href": "/health"},
            {"key": "schedule", "label": "Scheduled Tasks", "href": "/health/schedule"},
            {"key": "restarts", "label": "Restart History", "href": "/health/restarts"},
            {"key": "system", "label": "Logins & Backups", "href": "/health/system"},
            {"key": "reconnect", "label": "Reconnect", "href": "/reconnect"},
        ],
    },
    {
        "key": "agent",
        "label": "Agent",
        "href": "/agent",
        "pages": [
            {"key": "live", "label": "Live Status", "href": "/agent"},
            {"key": "usage", "label": "Usage", "href": "/agent/usage"},
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
        "key": "wellness",
        "label": "Wellness",
        "href": "/wellness",
        "pages": [
            {"key": "today", "label": "Today", "href": "/wellness"},
            {"key": "trends", "label": "Trends", "href": "/wellness/trends"},
            {"key": "drivers", "label": "Drivers", "href": "/wellness/drivers"},
            {"key": "journal", "label": "Journal", "href": "/wellness/journal"},
        ],
    },
    {
        "key": "habits",
        "label": "Habits",
        "href": "/habits",
        "pages": [
            {"key": "habits", "label": "Streaks", "href": "/habits"},
            {"key": "analysis", "label": "Analysis", "href": "/habits/analysis"},
        ],
    },
    {
        "key": "dayrating",
        "label": "Day Rating",
        "href": "/rating",
        "pages": [
            {"key": "today", "label": "Today", "href": "/rating"},
            {"key": "insights", "label": "Insights", "href": "/rating/insights"},
        ],
    },
    {
        "key": "chores",
        "label": "Chores",
        "href": "/chores",
        "pages": [
            {"key": "overview", "label": "Today", "href": "/chores"},
            {"key": "manage", "label": "Chores & kids", "href": "/chores/manage"},
            {"key": "ledger", "label": "Money", "href": "/chores/ledger"},
        ],
    },
    {
        "key": "activity",
        "label": "Activity",
        "href": "/activity",
        "pages": [{"key": "feed", "label": "Agent Activity", "href": "/activity"}],
    },
]

# True while serving an /archive/... request (set by the middleware in app.py). Archived requests run the
# same route handlers against the same data, but render the frozen pre-redesign templates in
# templates/archive/ with LEGACY_SECTIONS. Remove with the archive (docs/TODO.md).
ARCHIVE: ContextVar[bool] = ContextVar("dashboard_archive", default=False)


class _Templates(Jinja2Templates):
    def get_template(self, name):
        if ARCHIVE.get() and not name.startswith("archive/"):
            try:
                return super().get_template("archive/" + name)
            except TemplateNotFound:  # a page added after the redesign: show the current version
                pass
        return super().get_template(name)


templates = _Templates(directory=str(HERE / "templates"))


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


def fmt_date(value, empty="never"):
    """'Sep 8, 2025' for date-only values (no time of day)."""
    dt = _to_datetime(value)
    if dt is None:
        return empty if not value else str(value)
    return dt.strftime("%b %-d, %Y")


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
    fmt_dt=fmt_dt, fmt_date=fmt_date, fmt_duration=fmt_duration, fmt_minutes=fmt_minutes,
    status_pill=status_pill, stars=stars, fmt_bytes=fmt_bytes,
)
templates.env.globals["APP_NAME"] = APP_NAME


def asset_version() -> str:
    """Newest mtime under static/, appended to CSS/JS URLs so browsers never pair new HTML
    with a cached old stylesheet."""
    try:
        return str(int(max(f.stat().st_mtime for f in (HERE / "static").rglob("*") if f.is_file())))
    except (OSError, ValueError):
        return "0"


templates.env.globals["asset_version"] = asset_version


def find_page(section: str, page: str):
    """The (tab, page) of the navigation that holds a router's (section, page) pair."""
    src = f"{section}:{page}"
    for s in SECTIONS:
        for p in s["pages"]:
            if p.get("src") == src:
                return s, p
    return SECTIONS[0], None


def render(request: Request, template: str, section: str, page: str,
           title: str, status_code: int = 200, **context):
    """Render a page inside base.html with navigation state."""
    if ARCHIVE.get():
        current = next((s for s in LEGACY_SECTIONS if s["key"] == section), LEGACY_SECTIONS[0])
        nav = {"sections": LEGACY_SECTIONS, "section": current, "page": page,
               "archive_live_href": request.scope.get("archive_live_path", "/")}
    else:
        tab, current_page = find_page(section, page)
        nav = {"sections": SECTIONS, "section": tab, "current_page": current_page, "page": page}
    return templates.TemplateResponse(
        request, template, {**nav, "page_title": title, **context}, status_code=status_code,
    )
