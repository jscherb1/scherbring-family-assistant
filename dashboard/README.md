# Scherbring Family Assistant dashboard

A read-only web app (FastAPI + Jinja templates + htmx) for the family assistant. It reads
the state the assistant already keeps in `../state/` and never writes to it.

## Sections

| Section | Page | URL | What it shows |
|---|---|---|---|
| Orchestrator Health | Overview | `/health` | Uptime, task success rate, watchdog health, Telegram fallback, recent failures (auto-refreshes every 30 s) |
| | Scheduled Tasks | `/health/schedule` | Every scheduled task: what it does, schedule in plain English, next run, last run, 7-day ok/failed, recent runs |
| | Restart History | `/health/restarts` | Every orchestrator launch with the inferred cause, previous exit code, downtime, and the raw log records behind it |
| Meal Planner | Recipes | `/meals`, `/meals/<id>` | Browse, search, filter and sort the recipe database; view a recipe (view-only for now) |

JSON for the same data lives under `/api/*` (`/api/status`, `/api/schedule`, `/api/restarts`,
`/api/recipes`, `/api/recipes/<id>`), with interactive docs at `/api/docs`.

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

- Read-only: the app opens `state/agent_results.db` in SQLite read-only mode and only reads
  files under `state/`. There are no write routes.
- Restart causes are inferred. Only the launch type (`wrapper-start` / `loop-restart`) is
  recorded directly; the cause comes from matching timestamps in the exit log and the watchdog
  logs. Logs are pruned after 14 days, so older restarts show no watchdog detail.
- The scheduler's "ok" status means the run process exited successfully, not that the Telegram
  message was delivered.
- Tests: `.venv/bin/python -m pytest tests/test_dashboard.py`.
- Stop a foreground run with Ctrl+C.
