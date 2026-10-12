# Scherbring Family Assistant dashboard

A web app (FastAPI + Jinja templates + htmx) for the family assistant. It reads the state the
assistant already keeps in `../state/` and never writes to it, with one exception: the one-shot
"ask" box on the Summary page (below). It installs to a phone's home screen (web manifest and
icons in `static/icons/`, no service worker).

## Summary page and the ask box

`/` is a minimal landing page: the ask box, one status line ("All good" or "N things need
attention" with links), and one glance tile per section. `/api/summary` is its JSON twin.
`data/summary.py` only composes the existing readers.

Each needs-attention item has an ✕ to dismiss it. A dismissal is tied to the specific event (a
failed run's timestamp, a login's last-auth time, a maintenance item's due date), so it stays hidden
but a new failure or expiry shows up again. A "N dismissed" link under the list restores items.
Stored in `state/dashboard_dismissed.json`; entries are dropped once the condition clears.

The ask box (`routers/ask.py`, `POST /ask`) is deliberately not a chat app. It queues the text in
`state/inbox.db` as a Telegram-channel message for the owner's chat (`DASHBOARD_ASK_CHAT_ID`,
default: the first `BRIDGE_ALLOWED_CHAT_IDS` entry) with `via="web"` in the payload. The agent
answers in Telegram through the normal bridge, in the same conversation, so there is nothing to
keep in sync. Guards: Tailscale-only bind, same-origin check, 2000-character cap, 10 messages per
minute. `POST /api/ask` (form field or JSON `text`, returns `{"ok","message"}`) is the same path for
the iOS Shortcut: tagged `via="ios"`, no same-origin check (non-browser client). It assumes a single user (Justin); add real auth and a user mapping before opening it to
other family members.

## Sections

| Section | Page | URL | What it shows |
|---|---|---|---|
| Orchestrator Health | Overview | `/health` | Uptime, task success rate, watchdog health, Telegram fallback, recent failures (auto-refreshes every 30 s) |
| | Scheduled Tasks | `/health/schedule` | Every scheduled task: what it does, schedule in plain English, next run, last run, 7-day ok/failed, recent runs |
| | Restart History | `/health/restarts` | Every orchestrator launch with the inferred cause, previous exit code, downtime, and the raw log records behind it |
| | Logins & Backups | `/health/system` | Days until the Hy-Vee/Drive/Monarch logins expire, newest backup age and files, scheduler heartbeat |
| Agent | Live Status | `/agent` | Telegram bridge and agent worker liveness (heartbeats, Telegram polling, MCP servers, health-check result), queue depth, current turn, live event stream (refreshes every 5 s). Message text, tool inputs and results are **hidden** unless `DASHBOARD_SHOW_AGENT_CONTENT=1`; use `scripts/agent_ctl.py tail` for the full stream |
| | Usage | `/agent/usage` | Per-day turns, tokens (fresh/cache) and API-rate cost from the worker's turn log, plus current context size |
| Home & Yard | Maintenance | `/home` | Overdue / due soon / on-track items, last done, recent completions |
| | Lawn & Garden | `/home/lawn` | 6-step program progress, treatments, issues, products on hand, plants |
| | Weather Alerts | `/home/weather` | Alerts sent (7/30 days, by type), thresholds (coordinates hidden) |
| People & Family | Profile | `/family` | Upcoming birthdays, directory, facts on file by key, what's missing. **Fact values are not shown** |
| | Kids Memories | `/family/memories` | Counts per child/month/tag, Drive sync health. **Memory text is not shown** |
| Finance | Tagging | `/finance` | WHO-tagging activity, learned mapping, rule proposals. **No amounts or per-transaction merchants** |
| | Reports & Planning | `/finance/reports` | Report history with Drive links, advisor-profile completeness, non-monetary retirement settings |
| Hy-Vee | Overview | `/hyvee` | Purchase history, most-bought items, learned preferences, cart builds |
| Fitness | Overview | `/fitness` | This week's plan, COROS push status, library. Planned workouts only (no completion tracking) |
| Meal Planner | Recipes | `/meals`, `/meals/<id>` | Browse, search, filter and sort the recipe database; view a recipe |
| | Menu & Insights | `/meals/plan` | Upcoming menu (from the meal-planner's logged plans), recently cooked, stale recipes, collection counts |
| Habits | Streaks | `/habits` | Habit cards with current/longest streak and progress this period, one-tap Done, Undo, Pause, add-a-habit form, owner filter. Writes go through the shared `scripts/habits.py` service (same code as the `habits` agent) |
| Activity | Agent Activity | `/activity` | Latest agent results and last run per agent; sensitive agents' summaries are hidden |

Everything is view-only except the Habits page (see Notes). Because there is no login, sensitive areas (finance, kids memories, profile)
show summaries only.

JSON for the same data lives under `/api/*` (`/api/status`, `/api/schedule`, `/api/restarts`, `/api/system`,
`/api/recipes`, `/api/home/*`, `/api/family/*`, `/api/finance/*`, `/api/hyvee`, `/api/fitness`, `/api/meals/plan`, `/api/habits`, `/api/activity`), with interactive docs at `/api/docs`.

## Run it

From the repo root, using the shared virtualenv (`scripts/setup_wsl.sh`):

```
.venv/bin/python -m uvicorn dashboard.app:app --host 127.0.0.1 --port 5151
```

Environment variables used by the systemd unit:

- `DASHBOARD_HOST` - address to bind. Use this machine's Tailscale IP
  (`tailscale ip -4`) to reach it from other devices; avoid `0.0.0.0`.
- `DASHBOARD_PORT` - port (default 5151).

To keep it running, install and enable the user unit:

```
scripts/install_systemd.sh assistant-dashboard.service
```

To change the bind address, add a drop-in (`systemctl --user edit assistant-dashboard`) that
sets `Environment=DASHBOARD_HOST=...`, then `systemctl --user restart assistant-dashboard`.

## Layout

```
dashboard/
  app.py          FastAPI app; includes the routers
  web.py          templates, filters, and the SECTIONS navigation registry
  data/           read-only data layer (no web-framework imports; unit-tested)
  routers/        one router per section (HTML pages + /api/* JSON)
  templates/      base.html shell + one folder per section
  static/         style.css (design tokens, light/dark), htmx.min.js, dashboard.js
```

To add a section: create a router in `routers/`, include it in `app.py`, add templates, and add
an entry to `SECTIONS` in `web.py` (that entry drives the navigation).

## Notes

- New features follow the **"New feature standard"** in the repo `CLAUDE.md`: the data module and write
  routes call one shared service in `scripts/` (as `routers/habits.py` does), never their own copy of the rules.
- Read-only except `POST /ask` and the habits write routes (`POST /habits...`, same-origin guarded, going
  through `scripts/habits.py`): the app opens `state/agent_results.db` in SQLite read-only mode
  and only reads files under `state/`; the ask box is the sole write (one row in `state/inbox.db`).
- Mobile: below 760px the section pills collapse into a "Menu" dropdown, sub-nav scrolls
  sideways, and `.tbl` tables stack into labelled cards (a small script in `base.html` copies
  each column header onto its cells).
- Restart causes are inferred. Only the launch type (`wrapper-start` / `loop-restart`) is
  recorded directly; the cause comes from matching timestamps in the exit log and the watchdog
  logs. Logs are pruned after 14 days, so older restarts show no watchdog detail.
- The scheduler's "ok" status means the run process exited successfully, not that the Telegram
  message was delivered.
- Tests: `.venv/bin/python -m pytest tests/test_dashboard.py`.
- Stop a foreground run with Ctrl+C.
