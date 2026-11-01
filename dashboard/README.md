# Scherbring Family Assistant dashboard

A web app (FastAPI + Jinja templates + htmx) for the family assistant. It reads the state the
assistant already keeps in `../state/` and never writes to it, with one exception: the one-shot
"ask" box on the Summary page (below). It installs to a phone's home screen (web manifest and
icons in `static/icons/`, no service worker).

## Navigation and look

Redesigned 2026-10-19 (PR 1 of the UX overhaul: navigation, theme, archive). Five tabs, **Today, Me, Family, Home,
Money**, plus **System** behind the gear icon. Each tab has a sub-nav with small group headings (for example Me:
Wellness / Log / Habits / Training). On phones the tabs become a bottom tab bar. The registry is `SECTIONS` in
`web.py`. Each page entry's `src` names the `(section, page)` pair its router passes to `render()`, so regrouping the
nav never touches a router. Page URLs did not change.

| Tab | Pages |
|---|---|
| Today | Today (`/`), Week (`/week`) |
| Me | Wellness Today/Trends/Drivers, Check-in, Journal, Insights, Habits Streaks/Analysis, Fitness |
| Family | Chores Today/Chores & kids/Money, Kid chart (`/kids`), Profile, Kids memories |
| Home | Menu, Recipes, Groceries (`/hyvee`), Maintenance, Lawn & garden, Weather alerts |
| Money | Overview, Tagging, Reports & planning |
| System (gear) | Status (`/health`), Scheduled tasks, Restarts, Logins & backups, Reconnect, Agent Live, Usage, Activity, Weekly review, Archive |

There is one dark "HUD" theme, taken from the old My Hub page. All colors are tokens in `:root` in `static/style.css`
(and `--w-*` chart tokens in `static/wellness.css`), and there is no light mode. Labels, headings and numbers use
monospace; prose and tables use the system sans font. The kid chart (`/kids`) keeps its own bright look (`static/kids.css`).

### Jump-to search and long tables

`static/ui.js` loads on every page:
- **Jump-to search.** Press `/`, or tap the magnifier in the header, to fuzzy-find any page, a few common actions, recipes, habits
  and people. `/api/nav-index` (`data/navindex.py`) builds the list from `SECTIONS` and the existing readers. People link to
  `/family#q=Name`, which pre-fills the directory filter.
- **Long tables.** `<table data-limit="N">` shows the first N rows and a "Show all" button. `data-filter="placeholder"` adds a
  filter box. Expanded state survives htmx refreshes (`data-key` names a table that is re-rendered, like the Agent stream). Used on
  Groceries, Activity, Agent Live, People, Lawn, Finance, Menu and Maintenance.

### Archive (temporary)

`/archive` lists every page as it was before the redesign, and `/archive/<path>` serves that old version. A
middleware in `app.py` strips the prefix, sets `web.ARCHIVE`, and runs the **same route handler** against the same
data. Templates then resolve to the frozen copies in `templates/archive/`, with `LEGACY_SECTIONS` and
`static/archive/*`. In-app links and redirects get the `/archive` prefix, and archived pages still save
through the normal write routes. To remove the archive, delete `templates/archive/`, `static/archive/`,
`routers/archive.py`, the middleware, `ARCHIVE`/`LEGACY_SECTIONS` in `web.py` and the archive tests (tracked in
`docs/TODO.md`).

## Today page and the ask box

`/` is the Today page (see the Sections table). Its glance tiles and the agent/login-expiry attention items come from
`data/summary.py`, which only composes the existing readers; `/api/summary` is that reader's JSON twin. The old Summary
page is at `/archive/`.

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
| System | Status | `/health`, `/api/system/status` | One "is everything OK?" board: services (bridge, worker, MCP, health check, reply delivery, scheduler, backups), each integration's last connection check, login expiry, task success rate, recent failures and what runs next (refreshes every minute). Composes `data/health.py`, `data/agent.py`, `data/ops.py`, `data/schedule.py` (`data/status.py`) |
| | Scheduled Tasks | `/health/schedule` | Every scheduled task: what it does, schedule in plain English, next run, last run, 7-day ok/failed, recent runs |
| | Reconnect | `/reconnect`, `/reconnect/hyvee` | Refresh an expired Drive login (paste flow); shows Hy-Vee status and points at the desktop "Hy-Vee Login" shortcut (the only Hy-Vee sign-in). Write routes use the same-origin guard; same `scripts/reauth.py` service as the agent |
| | Logins & Backups | `/health/system` | Days until the Hy-Vee/Drive/Monarch logins expire, newest backup age and files, scheduler heartbeat |
| Agent | Live Status | `/agent` | Telegram bridge and agent worker liveness (heartbeats, Telegram polling, MCP servers, health-check result), queue depth, current turn, live event stream (refreshes every 5 s). Message text, tool inputs and results are **hidden** unless `DASHBOARD_SHOW_AGENT_CONTENT=1`; use `scripts/agent_ctl.py tail` for the full stream |
| | Usage | `/agent/usage` | Per-day turns, tokens (fresh/cache) and API-rate cost from the worker's turn log, plus current context size |
| Home & Yard | Maintenance | `/home` | Overdue / due soon / on-track items, last done, recent completions |
| | Lawn & Garden | `/home/lawn` | 6-step program progress, treatments, issues, products on hand, plants |
| | Weather Alerts | `/home/weather` | Alerts sent (7/30 days, by type), thresholds (coordinates hidden) |
| Home Assistant | Family Board | `/home/ha`, `/api/ha/board` | Live Scherbring Family HA dashboard in five tabs (Home, Climate, Media & lights, Security, Calendar; last tab remembered) under a chip strip (alarm, weather, garage, kid door alerts switch, who's home, low batteries and updates linking to HA health). Thermostats are ring gauges (current temp filled, dot at the setpoint, +/- and mode buttons). Writes (`POST /home/ha/act`, same-origin guarded) are limited to the allow-list in `scripts/ha.py`; alarm, garage doors and locks are view-only |
| | HA Health | `/home/ha/health`, `/api/ha/health` | Needs-attention list (notifications, unavailable devices by integration, low batteries, open doors, pending updates, stale automations, backups), recommendations, update list, recent automation runs, trend. Each item has a ✕ (dismiss) and a Dismissed list with Restore, stored in the shared `hub_dismissed` table under `ha:` keys (`POST /home/ha/dismiss`, `/home/ha/restore`); reference lists (unavailable, updates, batteries, automations, trend) are collapsed. Same `scripts/ha.py` service as the `ha-overview` agent |
| People & Family | Profile | `/family` | Upcoming birthdays, directory, facts on file by key, what's missing. **Fact values are not shown** |
| | Kids Memories | `/family/memories` | Counts per child/month/tag, Drive sync health. **Memory text is not shown** |
| Finance | Overview | `/finance` | Budget vs plan (fixed / flexible / non-monthly / total) with red/yellow/green, projected month end, spending vs last month and normal, savings rate, emergency-fund runway, card balances, net worth, over-budget categories, retirement verdict, review/no-WHO counts, bank connections. From the Monarch snapshot (`scripts/money.py`); **Refresh now** (`POST /finance/refresh`) starts a quick sync. Shows dollar amounts |
| | Tagging | `/finance/tagging` | Live needs-review and no-WHO-tag counts with the oldest few (merchant, amount), then the agent's WHO-tagging history, learned mapping and rule proposals |
| | Reports & Planning | `/finance/reports` | Report history with Drive links, advisor-profile completeness, non-monetary retirement settings |
| Hy-Vee | Overview | `/hyvee` | Purchase history, most-bought items, learned preferences, cart builds |
| Fitness | Overview | `/fitness` | This week's plan, COROS push status, library. Planned workouts only (no completion tracking) |
| Meal Planner | Recipes | `/meals`, `/meals/<id>` | Browse, search, filter and sort the recipe database; view a recipe; add, edit and delete recipes (same service as the meal-planner agent, `scripts/recipes.py`) |
| | Menu & Insights | `/meals/plan` | Upcoming menu (from the meal-planner's logged plans), recently cooked, stale recipes, collection counts |
| Today | Today | `/`, `/api/today` | The home page (Summary + My Hub merged): ask box, ranked needs-attention list, today's calendar/tasks/dinner, a quick 1-10 rating (full check-in on `/checkin`), weather, kids' chores, body (recovery, sleep, habits), the week ahead, and one row of glance tiles per tab. `data/today.py` composes `scripts/myhub.py` (cards, attention, dismissals) with `data/summary.py` (tiles plus the agent and login-expiry items the hub doesn't raise). `/me` redirects here; `/api/me` and `/api/summary` stay |
| Wellness | Today | `/wellness` | Whoop-style readiness: recovery / sleep / 7-day load rings, last night's sleep and stages, vitals against your 30-day normal, the day rating (yesterday's until today's is given in the evening), habits and source freshness. Same `scripts/wellness.py` service as the `wellness` agent |
| | Trends | `/wellness/trends` | 30/90/180-day SVG charts (sleep, HRV with its normal band, resting HR, load and load ratio, steps, exercise, mood) with hover tooltips / tap-to-pin and a 6-month calendar heatmap. Pending days show as dashed stubs, never zeros |
| | Drivers | `/wellness/drivers` | What moves the day rating, HRV, sleep score or resting HR (`insights.analyze` / `analyze_against`), a two-metric scatter, and the latest written story |
| Today | Week | `/week`, `/api/week` | The next 7 days, one column per day: calendar events, dinner, tasks, planned workouts, maintenance due and birthdays, with anything overdue on top. `data/week.py` composes the hub snapshot (`scripts/myhub.py`), the meal-planner menu (`data/activity.py`), this week's fitness plan and the profile birthdays |
| Me | Check-in | `/checkin` | The one place to log a day: rating + note, yes/no per journal question, daily habits, for today, yesterday (`?date=yesterday`) or any date; plus rating averages/streak and recent days. Same `scripts/journal.py` / `scripts/dayrating.py` services as the agents. `/rating` and `/wellness/journal?date=` redirect here |
| | Journal | `/wellness/journal` | A 28-day yes/no/unanswered grid (tap a day to open it in Check-in) and adding / pausing questions. Same `scripts/journal.py` service as the `journal` agent; the check-in card also sits on Wellness → Today and Drivers shows each question's yes-vs-no impact |
| Me | Insights | `/rating/insights` | The stats on what moves the day rating (`scripts/insights.py`), the AI-written weekly story and the data-source list |
| Golf | Rounds | `/golf`, `/api/golf` | Current handicap, last round, best score, 10-round average, a log-a-round form (course autofills rating/slope), recent rounds with which ones count toward the handicap, undo, and a retry for sheet sync. Writes go through the shared `scripts/golf.py` service (same code as the `golf` agent) |
| Golf | Courses | `/golf/courses`, `/api/golf/courses` | Saved course rating/slope used to autofill new rounds; add or update a course |
| Habits | Analysis | `/habits/analysis` | Combined habits-done-per-day calendar (3 months) and a per-habit table (days done, last 30 days, best weekday, weekly strip); the place to grow cross-habit analysis. Same `scripts/habits.py` service (`analysis`) |
| Habits | Streaks | `/habits` | Habit cards with current/longest streak and progress this period, one-tap Done, Undo, Pause, a 📅 Calendar flip on each card (2 months of days done), add-a-habit form, owner filter. Writes go through the shared `scripts/habits.py` service (same code as the `habits` agent) |
| Chores | Today, Chores & kids, Money | `/chores`, `/chores/manage`, `/chores/ledger` | Adult setup for the kid chore chart: who's done what today, kids/chores/schedules/values, balances and payouts, and fixing a missed or mistaken completion. Writes go through the shared `scripts/chores.py` service (same code as the `chores` agent) |
| (kids) | Chore Chart | `/kids` | Kid-facing page (no adult nav; linked from Family → Kid chart): big tap-to-complete chores for each kid with balance, streaks, confetti/chime, week strip and badges |
| (kids) | One kid's chart | `/kids/<name>` | Same page as `/kids` filtered to a single kid (e.g. `/kids/ruth`), for that kid's tablet bookmark. Hides the other kid's chart but is not access control (no login) |
| System | Weekly review | `/review`, `/api/review` | The latest step-back review summary and its findings (title, why it matters, evidence, one suggested action) with Accept / Done / Dismiss / Reopen. Same `scripts/review.py` service as the `review` agent; new findings show on the Summary as a "Review" tile and `review:<id>` attention items. Never shows kids' memory text or profile values |
| Activity | Agent Activity | `/activity` | Latest agent results and last run per agent; sensitive agents' summaries are hidden |

Everything is view-only except the Habits page (see Notes). Because there is no login, sensitive areas (kids memories, profile,
finance report text and advisor/retirement profile values) show summaries only. The Money overview and the live review lists do
show dollar amounts and merchants (decided 2026-10-21: the dashboard is only reachable on localhost/Tailscale).

JSON for the same data lives under `/api/*` (`/api/status`, `/api/schedule`, `/api/system`,
`/api/recipes`, `/api/home/*`, `/api/family/*`, `/api/finance/*`, `/api/hyvee`, `/api/fitness`, `/api/meals/plan`, `/api/habits`, `/api/ha/*`, `/api/wellness*`, `/api/journal/*`, `/api/chores*`, `/api/activity`), with interactive docs at `/api/docs`.

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
  templates/      base.html shell + one folder per section; archive/ = frozen pre-redesign templates
  static/         style.css (dark HUD design tokens), htmx.min.js; archive/ (also holds the old dashboard.js) = frozen old CSS/JS
```

To add a section: create a router in `routers/`, include it in `app.py`, add templates, and add
a page entry (with `src` = the router's `"section:page"`) under the right tab in `SECTIONS` in `web.py`.

## Notes

- New features follow the **"New feature standard"** in the repo `CLAUDE.md`: the data module and write
  routes call one shared service in `scripts/` (as `routers/habits.py` does), never their own copy of the rules.
- Read-only except `POST /ask`, the habits write routes (`POST /habits...`), the golf write routes (`POST /golf...`), the day-rating write route (`POST /rating`) and the chore-chart write routes
  (`POST /chores...`, `POST /kids/...`) and the Home Assistant routes (`POST /home/ha/act`, `/dismiss`, `/restore`), all same-origin guarded and going through `scripts/habits.py` /
  `scripts/chores.py`: the app opens `state/agent_results.db` in SQLite read-only mode
  and only reads files under `state/`; the ask box is the sole write (one row in `state/inbox.db`).
- Mobile: below 760px the tabs move to a bottom tab bar, the sub-nav scrolls
  sideways, and `.tbl` tables stack into labelled cards (a small script in `base.html` copies
  each column header onto its cells).
- The scheduler's "ok" status means the run process exited successfully, not that the Telegram
  message was delivered.
- Tests: `.venv/bin/python -m pytest tests/test_dashboard.py`.
- Stop a foreground run with Ctrl+C.
