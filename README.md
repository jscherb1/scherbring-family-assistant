# Personal AI Assistant — Phase 1 (Telegram → Orchestrator → Todoist subagent)

A personal multi-agent assistant orchestrated through Claude Code, running locally on
Windows. One orchestrator is the single point of contact; it delegates to scoped
subagents. Phase 1 proves the core loop end-to-end with one real subagent (**Todoist**)
and a shared state store that gives follow-up questions continuity.

```
You (Telegram) → Telegram channel plugin → Orchestrator (Claude Code, local)
                                                   │ delegates via Task tool
                                                   ▼
                                          Todoist subagent  ── Todoist MCP (tasks/lists)
                                                   │
                                                   ▼ writes/reads
                                          Shared state store (SQLite)  → orchestrator replies in Telegram
```

## Layout

| Path | Purpose |
|------|---------|
| `.claude/agents/todoist.md` | Todoist subagent (scoped to Todoist MCP + the state store) |
| `.claude/agents/meal-planner.md` | Meal-planner subagent (recipe library, Google Calendar, Todoist Shopping List + frozen-protein thaw reminders) |
| `.claude/agents/scheduler.md` | Scheduler subagent — creates/lists/pauses/deletes proactive scheduled tasks |
| `.claude/agents/kids-memory.md` | Kids-memory subagent — captures memories about the kids locally + to Google Drive |
| `.claude/agents/lawn-garden.md` | Lawn & garden subagent — tracks the lawn care program, product inventory, plants, weed issues, and the weekly proactive weather/calendar check |
| `.claude/agents/profile.md` | Profile subagent — captures/recalls structured facts about the user, family, and friends; other subagents read the store directly |
| `.claude/agents/weather-reminders.md` | Weather-reminders subagent — daily proactive forecast check (rain → deck cushions, snow → shoveling prep, severe weather watches) plus ad hoc weather questions |
| `.claude/agents/home-maintenance.md` | Home-maintenance subagent — recurring indoor maintenance items (HVAC filters, smoke detector batteries, etc.), completion log, and the weekly proactive due-check |
| `.claude/agents/home-assistant.md` | Home-assistant subagent (Phases 1-3) — ad-hoc smart-home control/query via the hosted Home Assistant connector plus a local `ha` MCP server (vendor/ha-mcp) for automation/script/scene/helper/dashboard authoring and history/log debugging; proactive monitoring is a later phase |
| `vendor/ha-mcp/` | Vendored [`homeassistant-ai/ha-mcp`](https://github.com/homeassistant-ai/ha-mcp) (PyPI package, `.venv`) — runs as a loopback-only local HTTP server (`scripts/start_ha_mcp.ps1`) that Claude connects to via the `ha` entry in `.mcp.json` |
| `.claude/agents/fitness.md` | Fitness subagent (Phases 1-3) — plans weekly workouts by reusing the COROS Training Hub library, gated on approval, writes to Coros + the "Running" Google Calendar |
| `scripts/fitness_store.py` | Zero-dep CLI for fitness data (workout-library cache sync/list, weekly-plan add/list/update sub-commands) |
| `scripts/fitness/` | Playwright + API browser layer for COROS Training Hub — `login_test.py` (session), `library_sync.py` (Workout Library scrape), `schedule_ops.py` (list-week / add-existing-workout / remove-workout, via the real Coros API, not drag-and-drop), `coros_web.py` (selector/URL/endpoint constants), `coros_session.py` (shared helpers) |
| `.claude/agents/finance.md` | Finance subagent (Phase 1) — Monarch transaction review + WHO tagging, rule proposals, historical sweeps |
| `.claude/agents/finance-reporter.md` | Finance-reporter subagent (Phases 2 & 4) — weekly/monthly/annual spending-summary HTML reports to Drive + Telegram brief |
| `.claude/agents/retirement.md` | Retirement subagent (Phase 3) — Monte Carlo projection + editable `.xlsx` modeling workbook to Drive; owns "can we afford X" what-ifs |
| `.claude/agents/finance-advisor.md` | Finance-advisor subagent (Phase 5) — on-demand, read-only proactive recommendations engine (idle cash, debt, tax-advantaged, tax-loss, insurance/estate + directional ideas) |
| `scripts/finance_store.py` | Zero-dep CLI for finance working-state (who-map / tagging log / rule proposals / report log / config sub-commands) |
| `scripts/finance_report.py` | Renders self-contained HTML spending reports (`--period weekly|monthly|annual`) |
| `scripts/retirement_model.py` | Monte Carlo retirement engine (numpy) — success probability, percentile bands, base + what-if scenarios |
| `scripts/retirement_workbook.py` | Builds the user-editable `.xlsx` retirement modeling workbook (xlsxwriter) |
| `scripts/drive_upload.py` | Path-based Google Drive upload/in-place-update CLI (own OAuth token) for the binary `.xlsx` workbook |
| `.mcp.json` | Project MCP config (Todoist HTTP/OAuth). Gitignored. See `.mcp.json.example`. |
| `state/schema.sql` | SQLite schema for `agent_results`, `recipes`, `scheduled_tasks`, `scheduled_task_runs`, `kid_memories`, `kid_memory_triggers`, `lawn_garden_plants`, `lawn_garden_products`, `lawn_garden_program`, `lawn_garden_treatments`, `lawn_garden_issues`, `lawn_garden_config`, `profile_people`, `profile_people_facts`, `profile_facts`, `weather_config`, `weather_alerts`, `home_maintenance_items`, `home_maintenance_completions`, `hyvee_purchase_history`, `hyvee_item_prefs`, `hyvee_feedback_log`, `hyvee_cart_runs`, `finance_who_map`, `finance_tag_log`, `finance_rule_proposals`, `finance_report_log`, `finance_config`, `fitness_workout_library`, `fitness_weekly_plans` |
| `state/agent_results.db` | The state store (auto-created; gitignored — holds personal data) |
| `scripts/state_store.py` | Zero-dep CLI the subagents call to write/read continuity results |
| `scripts/recipes_store.py` | Zero-dep CLI for the recipe library (list/add/feedback/mark-cooked) |
| `scripts/scheduler_store.py` | Zero-dep CLI for the scheduled-tasks registry (add/list/enable/disable/delete/due/log-run); called by `scheduler_dispatch.py` to find and dispatch due tasks |
| `scripts/scheduler_dispatch.py` | Zero-token external poller — writes a heartbeat tick, checks for due tasks, and dispatches each via `claude --print` as a one-shot subprocess. Exits silently when nothing is due. |
| `scripts/run_scheduler_hidden.vbs` | VBS launcher so the Windows Task Scheduler trigger runs without a console flash (same pattern as `run_watchdog_hidden.vbs`) |
| `scripts/register_scheduler_task.ps1` | One-time setup for the `PersonalAssistantScheduler` Scheduled Task (fires every ~2 min, runs `scheduler_dispatch.py`) |
| `scripts/start_orchestrator.ps1` | Idempotent launcher — skips if already running, restarts on exit |
| `scripts/orchestrator_status.ps1` | Read-only check for whether the orchestrator is running |
| `scripts/register_orchestrator_task.ps1` | One-time setup for the auto-start-at-logon Scheduled Task |
| `scripts/start_ha_mcp.ps1` | Idempotent launcher for the local `ha-mcp` HTTP server (loopback-only, port 8086) — skips if already running, restarts on exit |
| `scripts/run_ha_mcp_hidden.vbs` | VBS launcher so the ha-mcp auto-start-at-logon task runs without a console flash |
| `scripts/register_ha_mcp_task.ps1` | One-time setup for the `PersonalAssistantHaMcp` auto-start-at-logon Scheduled Task |
| `scripts/watchdog_telegram_health.ps1` | Detects a stuck Telegram MCP connection and force-restarts the orchestrator |
| `scripts/watchdog_scheduler_health.ps1` | Detects a stalled `PersonalAssistantScheduler` dispatch task (via heartbeat file), triggers recovery, and sends a direct Telegram alert |
| `scripts/run_watchdog.ps1` | Wrapper the watchdog Scheduled Task invokes; logs to `state/logs/` |
| `scripts/register_watchdog_task.ps1` | One-time setup for the watchdog Scheduled Task (fires every ~2 min) |
| `scripts/kid_memories_store.py` | Zero-dep CLI for kid memories (add/update/get/list/mark-drive-synced/mark-drive-failed/add-trigger/triggers) |
| `scripts/lawn_garden_store.py` | Zero-dep CLI for lawn & garden data (plant/product/program/treatment/issue/config sub-commands) |
| `scripts/profile_store.py` | Zero-dep CLI for the personal profile store (person/fact/global-fact sub-commands) — any subagent can call it directly |
| `scripts/weather_store.py` | Zero-dep CLI for weather-reminders data (config/alert sub-commands) |
| `scripts/home_maintenance_store.py` | Zero-dep CLI for home maintenance data (item/completion/due sub-commands) |
| `.claude/agents/hyvee.md` | Hy-Vee cart-builder subagent — resolves the Todoist shopping list to specific products and builds (never places) a Hy-Vee Aisles Online cart |
| `scripts/hyvee_store.py` | Zero-dep CLI for the cart-builder decision store (history / prefs / feedback / seed / resolve / rank / run sub-commands) |
| `scripts/hyvee/` | Playwright browser layer — `cart_ops.py` (login, purchase-history sync, product search, add-to-cart, cart verify), `diagnose.py` (site-change maintenance toolkit), `hyvee_web.py` (selector/URL/endpoint constants), `hyvee_session.py` (shared helpers) |
| `.env.example` | Env template (Hy-Vee cart builder needs `HYVEE_USERNAME`/`HYVEE_PASSWORD` here; otherwise no real secrets needed for Phase 1) |

> **Location matters:** this project lives **outside** OneDrive on purpose. The personal
> task DB and any tokens must not sync to a corporate cloud tenant. Back up via a
> **private** git remote, not OneDrive.

## Setup

### A. Manual, one-time (you — needs an interactive `claude` session + a browser)

1. **Telegram bot** — create one via [@BotFather](https://t.me/BotFather); copy the token.
2. **Your Telegram user ID** — get your numeric ID (e.g. via [@userinfobot](https://t.me/userinfobot)).
3. **Private git remote** (backup):
   ```
   git remote add origin <your-private-repo-url>
   git push -u origin main
   ```
4. **Todoist MCP** — `.mcp.json` is already present (official hosted server, OAuth).
   On first Todoist tool use, a browser sign-in launches automatically. Complete it.
   - Do **not** also run `claude mcp add todoist ...` — a duplicate entry causes OAuth loops.
   - Verify: `claude mcp list` should show `todoist` connected.
   - Use a Todoist **test project** if you want to avoid touching your real lists — the
     subagent can create and complete tasks.
5. **Install Bun** — the Telegram channel's MCP server runs on **Bun** (not Node). Without
   it the channel silently never starts. Install and reopen your terminal:
   ```powershell
   powershell -c "irm bun.sh/install.ps1 | iex"
   ```
   Verify: `bun --version`.
6. **Telegram channel** — configure the bot (any `claude` session, persists to
   `~/.claude/channels/telegram/.env`):
   ```
   /plugin install telegram@claude-plugins-official
   /telegram:configure <bot-token>
   ```
   Then **launch with the channel** (Step "Launch" below), and **pair your account** —
   this is the step that actually adds you to the allowlist:
   ```
   # 1. From Telegram, send any message to your bot → it replies with a pairing code.
   # 2. In the running Claude Code session:
   /telegram:access pair <code>
   # 3. Optionally lock down so only paired users get through:
   /telegram:access policy allowlist
   ```
   > `policy allowlist` alone does NOT add you — you must `pair` first, or every message
   > is silently dropped. Telegram channels are a research-preview feature; confirm command
   > names against the live docs: https://code.claude.com/docs/en/channels.md

### B. Already built (in this repo — no action needed)

- Todoist subagent definition, scoped to Todoist MCP tools + `Bash` (only to call the
  state store).
- SQLite state store + `state_store.py` CLI (verified: write/query round-trip works).
- `.gitignore` covering `.env`, tokens, `.mcp.json`, `settings.local.json`, and the DB.
- Scheduled-tasks feature (registry, poller, local channel, `scheduler` subagent) —
  see the dedicated **Scheduled tasks** setup section below for the one-time steps
  this one *does* need.
- Kids memory keeper (`kid_memories`/`kid_memory_triggers` tables, `kid_memories_store.py`,
  `kids-memory` subagent) — see the dedicated **Kids memory keeper** section below. Needs
  the Google Drive MCP connector authorized (already done) and no further setup.

## Launch (the orchestrator)

Preferred: run the idempotent launcher — it checks whether an instance is already
running before starting a new one, and restarts it automatically if it ever exits:

```
powershell -ExecutionPolicy Bypass -File scripts\start_orchestrator.ps1
```

Or run the raw command directly (no idempotency/restart):

```
claude --channels plugin:telegram@claude-plugins-official
```

Leave the session running; it's the live orchestrator. Message your bot from Telegram.

## Always-on: auto-start at logon + status check

One-time setup — registers a Scheduled Task (`PersonalAssistantOrchestrator`) that
starts the orchestrator automatically whenever you log into Windows, in a minimized
window, and restarts it if it crashes:

```
powershell -ExecutionPolicy Bypass -File scripts\register_orchestrator_task.ps1
```

- The task runs only while you're logged on (no Windows password stored) — it keeps
  going through a locked screen, but stops if you sign out or restart without logging
  back in.
- Minimize the window rather than closing it; closing the window stops the assistant.
- To check whether it's currently running (e.g. before manually starting another copy
  and accidentally double-polling Telegram):
  ```
  powershell -ExecutionPolicy Bypass -File scripts\orchestrator_status.ps1
  ```
- To re-register the task later (e.g. after moving the repo), just re-run
  `register_orchestrator_task.ps1` — it replaces the existing task.
- To remove it: `Unregister-ScheduledTask -TaskName PersonalAssistantOrchestrator`.
- Unattended auto-restart (crash recovery, logon) is safe to rely on: the orchestrator
  no longer loads any development channel at launch, so there's no interactive
  confirmation dialog left to silently stall a restart (see **Scheduled tasks** below
  for the history here).

## Scheduled tasks (proactive, recurring prompts)

Lets you ask the orchestrator to do something on a schedule — "every Sunday at 9am,
plan next week's dinners and post it" — and have the result posted into this same
Telegram chat. Adding a new scheduled task is just a conversation with the `scheduler`
subagent (or a row in the registry via `scheduler_store.py`) — it never touches
Windows Task Scheduler.

**Architecture:** A dedicated Windows Scheduled Task (`PersonalAssistantScheduler`)
runs `scripts/scheduler_dispatch.py` every 2 minutes. That script:
1. Writes a heartbeat tick to `state/scheduler_loop_state.json` (monitors health).
2. Runs `scheduler_store.py due` to check the SQLite registry for anything due.
3. **If nothing is due — exits immediately. Zero Claude API calls, zero tokens.**
4. If tasks are due — dispatches each via `claude --print "<prompt>" --channels
   plugin:telegram@claude-plugins-official` as a one-shot subprocess (fresh context,
   no accumulated session state), then logs the result.
5. If a task fails — sends a direct Telegram alert via the Bot API (no Claude involved).

A separate OS-level watchdog (`scripts/watchdog_scheduler_health.ps1`) verifies the
heartbeat is ticking and triggers recovery if it goes stale, suppressing alerts caused
by machine sleep (both heartbeat and task last-run equally stale = silent recovery).

*(History: v1 used a hand-written local MCP channel server requiring
`--dangerously-load-development-channels`, which caused a multi-hour outage from
unautomatable interactive confirmation dialogs. v2 used an in-session `CronCreate`
poll loop that consumed ~720 idle Claude turns/day even when nothing was scheduled.
The current external-dispatch approach eliminates both problems.)*

One-time setup:

1. **Register the scheduler Scheduled Task** (fires every ~2 minutes, zero tokens
   when idle):
   ```
   powershell -ExecutionPolicy Bypass -File scripts\register_scheduler_task.ps1
   ```
2. **Register the watchdog Scheduled Task** (Telegram-connection health check +
   scheduler heartbeat health check, runs every ~2 minutes):
   ```
   powershell -ExecutionPolicy Bypass -File scripts\register_watchdog_task.ps1
   ```
3. **Set the alert chat ID** the watchdog uses for direct Telegram alerts — edit
   `scripts/scheduler.config.json`:
   ```json
   { "alert_chat_id": "<your chat_id>" }
   ```
4. Try it: message the bot "every day at [a couple minutes from now], say hello" and
   confirm the scheduled reply arrives — no manual channel setup, no `bun install`,
   no confirmation dialogs.

## Kids memory keeper

Tell the orchestrator (in chat, or via Telegram) a quick memory, anecdote, or note
about the kids — Ruth (4) and Claire (7) — natural-language ("Claire said the
funniest thing today...") or explicit ("remember this for Ruth"). The `kids-memory`
subagent:

- Saves a local SQLite record (`kid_memories` table) that keeps the **verbatim raw
  text** (`raw_text`, never edited) separate from an optional cleaned-up/corrected
  working copy (`memory_text`).
- Resolves the memory's actual **event date** from the message (not just "today") —
  e.g. "back in March..." files correctly under that date, not the day it was
  captured — with `exact`/`approximate` precision tracking.
- Tags one or both children on a single record (no duplicate local rows for a
  memory about both kids).
- Mirrors a Markdown file (front-matter metadata + raw/refined text) into that
  child's subfolder under the linked [Google Drive
  folder](https://drive.google.com/drive/folders/1JP0kp_c6GcxwnRTlpAOGkwTEJjIgrEKS)
  for long-term archival, filed by event date.
- Learns which phrasings are/aren't real memory triggers over time
  (`kid_memory_triggers` table) to reduce missed captures and false positives.
- **Recall & chat**: ask it anything about past memories — "what do we know about
  Claire's swimming lessons?", "any updates on the girls lately?", "tell me about
  last summer" — with or without a timeframe. It infers child/keyword/date filters
  from the question, searches the local DB, and answers conversationally
  (follow-ups keep continuity via the shared state store, same pattern as
  meal-planner). Read-only — chatting never edits a memory.
- **Proactive, scheduled**: a monthly recap (first Sunday of the month, 7:30 PM —
  highlights per child from the past month) and a weekly capture-cadence check
  (Sunday 8 PM — nudges you if either girl hasn't had a memory logged in the past
  week; silent otherwise). Both registered as `scheduler` tasks
  (`kids-memory-monthly-recap`, `kids-memory-weekly-checkin`) — see **Scheduled
  tasks** above for how that delivery mechanism works. Note: since cron here can't
  natively express "first Sunday of the month," the monthly task actually fires
  every Sunday and self-gates internally on day-of-month 1–7.

Text-only for now; the schema (`tags_json`/`metadata_json` catch-alls, `media_type`
column) is built to extend to photos/audio later without a migration.

No setup needed beyond what's already done — the Google Drive MCP connector is
authorized, the `Ruth`/`Claire` subfolders already exist, and both scheduled tasks
are registered.

## Lawn & garden agent

Tracks the Reinders 6-Step lawn care program, the chemical/product inventory, plant &
mulch-bed locations, and recurring weed issues (quackgrass, clover, dandelion, thistle)
for the ~10,000 sq ft yard in Rochester, MN. The `lawn-garden` subagent:

- Logs treatments (program rounds and ad-hoc spot sprays) as they happen —
  `lawn_garden_treatments` — including product, method (`broadcast`/`spot-spray`/
  `backpack`), area, and target weeds.
- Keeps the product inventory (`lawn_garden_products`), plant/bed locations
  (`lawn_garden_plants`), and open weed/pest issues (`lawn_garden_issues`) up to date
  from natural-language notes ("picked up a jug of Roundup", "there's a new thistle
  patch by the shed").
- Answers recall questions ("when did we last spray?", "what's Round 2?") straight from
  the local SQLite data — read-only, same continuity pattern as the other subagents.
- **Proactive, scheduled**: a weekly check (Saturdays) that combines program timing,
  the ~monthly spot-spray cadence, current weather (Open-Meteo, no API key needed), and
  Google Calendar free/busy time into one consolidated reminder — silent if nothing is
  due. Register it via the `scheduler` subagent, same mechanism as the kids-memory
  scheduled tasks (see **Scheduled tasks** above):
  ```
  python scripts/scheduler_store.py add --name "lawn-garden-weekly-check" \
    --prompt "Delegate to the lawn-garden subagent to run the weekly proactive lawn & garden check." \
    --cron "0 8 * * 6" --target-chat-id "<your chat_id>"
  ```
- Calendar events are only created after you explicitly confirm a suggested task in
  chat — the weekly check itself never writes to your calendar unprompted.

One-time setup: seed the program and yard profile once —
```
python scripts/lawn_garden_store.py program seed
python scripts/lawn_garden_store.py config set --key yard_size_sqft --value 10000
python scripts/lawn_garden_store.py config set --key location --value "Rochester, MN"
python scripts/lawn_garden_store.py config set --key latitude --value 44.0234
python scripts/lawn_garden_store.py config set --key longitude --value -92.4630
python scripts/lawn_garden_store.py config set --key equipment --value "3x 1-gallon sprayer, 1x 4-gallon backpack sprayer"
```
then register the weekly check above whenever you're ready.

## Weather reminders agent

Proactive nudges ahead of incoming weather for the Rochester, MN home — bring in the
deck cushions/furniture before rain, get ahead of shoveling before a decent snowfall,
and flag severe thunderstorm/tornado watches. The `weather-reminders` subagent:

- Checks Open-Meteo (no API key needed) against three simple rules —
  `rain_cushions`, `snow_shoveling`, `severe_weather` — with thresholds stored in
  `weather_config` (rain probability/amount, snow inches) and overridable any time.
- **Proactive, scheduled**: a daily morning check that posts one consolidated message
  only when something new is coming — silent otherwise. Each newly-triggered event is
  logged in `weather_alerts` so it isn't repeated on consecutive mornings while still
  in the forecast window.
- Never creates a Todoist task unprompted — it proposes in chat, and only adds the
  task if you confirm in a later turn.
- Also answers ad hoc questions any time ("what's the weather looking like this
  week?", "should I worry about anything?") without touching the alert log.

One-time setup: seed the location once (reuses the same coordinates as the lawn &
garden agent, entered independently since each agent owns its own config) —
```
python scripts/weather_store.py config set --key latitude --value 44.0234
python scripts/weather_store.py config set --key longitude --value -92.4630
python scripts/weather_store.py config set --key location --value "Rochester, MN"
```
then register the daily check:
```
python scripts/scheduler_store.py add --name "daily-weather-check" \
  --prompt "Delegate to the weather-reminders subagent to run the daily proactive weather check (rain before cushions, snow-shoveling prep, severe weather watch)." \
  --cron "30 6 * * *" --target-chat-id "<your chat_id>"
```

## Home maintenance agent

Tracks recurring indoor home maintenance items — HVAC filter changes, smoke/CO
detector batteries, water softener salt, garage clean-outs, and seasonal chores
like winterizing outdoor spigots — each on its own recurrence cadence. The
`home-maintenance` subagent:

- Logs completions as they happen (`home_maintenance_completions`) against a set
  of recurring item definitions (`home_maintenance_items`, each with its own
  `interval_days`), same "definition + append-only log" shape as the lawn-garden
  agent's plants/treatments tables.
- Answers questions straight from the local data — "what's due soon?", "when did
  we last test the smoke detectors?" — read-only, same continuity pattern as the
  other subagents.
- **Proactive, scheduled**: a weekly check (Sundays) that flags anything overdue
  or due within the next 7 days in one consolidated message — silent if nothing's
  due. It then offers a Google Calendar event, a Todoist task, or both for any
  item; **nothing is created until you confirm**, and logging a completion always
  requires you to say the chore was actually done (a reminder alone never marks
  something complete).
- Seeded with a starter list you can edit/pause (`--active 0`)/extend anytime:
  HVAC filter change (90 days), smoke/CO detector battery test (~6 months), water
  softener salt check (30 days), garage clean-out (~6 months, spring & fall),
  winterize outdoor spigots (annual), test GFCI outlets (annual).

One-time setup: seed the starter items, then register the weekly check —
```
python scripts/home_maintenance_store.py item seed
python scripts/scheduler_store.py add --name "home-maintenance-weekly-check" \
  --prompt "Delegate to the home-maintenance subagent to run the weekly proactive home maintenance due-check." \
  --cron "0 9 * * 0" --target-chat-id "<your chat_id>"
```

## Home Assistant agent

Controls and queries Home Assistant (lights, climate, media, covers, locks) and, since
Phase 2/3, authors/edits automations, scripts, scenes, helpers, and dashboards, plus
history/log debugging ("why didn't X automation run"). The `home-assistant` subagent
uses two connections:

- The Anthropic-hosted Home Assistant connector (`mcp__claude_ai_Home_Assistant__*`,
  `Hass*` tools + `GetLiveContext`) for ordinary, low-friction control/query — no setup
  needed.
- A local, vendored **`ha-mcp`** server ([`homeassistant-ai/ha-mcp`](https://github.com/homeassistant-ai/ha-mcp),
  installed from PyPI into `vendor/ha-mcp/.venv`) for everything the hosted connector
  can't do — automation/script/scene/helper/dashboard CRUD, arbitrary entity search,
  and history/traces/logs. It runs as a **loopback-only local HTTP server**
  (`127.0.0.1:8086`, no LAN exposure) and is registered as `ha` in `.mcp.json`.

**Security-sensitive actuators** (locks, garage doors, door/garage-classed covers)
always require explicit confirmation before the subagent acts, even on a direct
chat request — stricter than most other subagents here, which only gate proactive
writes. Automation/script/scene/dashboard writes always follow propose-then-confirm.

One-time setup for the local `ha` server:
```
# 1. Home Assistant -> Profile -> Security -> Long-lived access tokens -> Create Token
# 2. Add to .env (gitignored, never commit):
#      HOMEASSISTANT_URL=http://homeassistant.local:8123
#      HOMEASSISTANT_TOKEN=<paste token>
# 3. Install the vendored package once:
python -m venv vendor/ha-mcp/.venv
./vendor/ha-mcp/.venv/Scripts/python.exe -m pip install ha-mcp
# 4. Register auto-start-at-logon (mirrors register_orchestrator_task.ps1):
powershell -ExecutionPolicy Bypass -File scripts\register_ha_mcp_task.ps1
# 5. Start it now without logging off/on:
powershell -ExecutionPolicy Bypass -File scripts\start_ha_mcp.ps1
```
Verify with `claude mcp list` — the `ha` entry should show **Connected**.

## Fitness agent

Plans weekly workouts (3-4 runs, 2 strength — including one lower-body-focused, 1
Peloton) by reusing existing workouts from the user's COROS Training Hub
(t.coros.com) library, gates the draft plan on user approval, then writes each
approved slot to both Coros and the "Running" Google Calendar. The `fitness`
subagent checks every calendar (not just "Running") for conflicts in a default
5:30-7:30am window before proposing times.

Coros has no public API, so `scripts/fitness/` automates it directly:

- `login_test.py` — logs in (Arco Design form, no separate identity subdomain) and
  persists a session to `state/coros_session.json`; falls back to fresh login with
  `COROS_USERNAME`/`COROS_PASSWORD` from `.env`. **Coros enforces a single active web
  session per account** — a fresh automated login will sign the user out of Coros
  elsewhere (phone, browser), and vice versa. Accepted as a known limitation; the
  automation self-heals via `.env` credentials either way.
- `library_sync.py` — scrapes the (virtualized — scrolls and merges, not a single
  query) Workout Library panel into `fitness_workout_library`
  (`scripts/fitness_store.py workout sync`), the source of truth for what can be
  scheduled without building anything new.
- `schedule_ops.py` — `list-week` (read-only), `add-existing-workout`,
  `remove-workout`. Scheduling deliberately does **not** use drag-and-drop — it was
  flaky (~2/8 success rate across both Playwright and real OS-level input, failing
  silently) — and instead replays the real API Coros's own frontend uses:
  `GET /training/program/detail` + `POST /training/schedule/update`, authenticated
  via an `accessToken` header sourced from the `CPL-coros-token` cookie (plain
  cookies alone 401). The next safe `idInPlan` is queried live from
  `GET /training/schedule/query`'s `maxIdInPlan` field rather than tracked locally,
  so it self-corrects even if the user schedules something via the Coros app
  directly.

Custom Coros workout creation (the "Create Workouts" interval builder) isn't
automated — the agent flags a gap and asks the user to build it manually rather than
guessing at a structure. History-informed planning (using
`fitness_weekly_plans.completion_status` to adjust future weeks) is schema-ready but
not yet built.

One-time setup: add `COROS_USERNAME`/`COROS_PASSWORD` to `.env`, then
`pip install -r scripts/fitness/requirements.txt && playwright install chromium`.
Weekly trigger: `weekly-fitness-plan` scheduled task, Sundays 6pm — see **Scheduled
tasks** above.

See `docs/superpowers/specs/2026-08-20-fitness-agent-coros-login-design.md` and its
two follow-on specs in the same directory for the full phased design and live
investigation notes.

## Personal profile

A single source of truth for structured facts about the user, family, and friends —
name, relationship, birthday, and open-ended per-person facts (allergy, shirt_size,
school, dietary preference, etc.), plus household-level facts not tied to one person
(home address, timezone, anniversary). Backed by `profile_people` /
`profile_people_facts` / `profile_facts` and `scripts/profile_store.py`.

- Talk to the `profile` subagent for natural-language capture ("remember my wife's
  birthday is March 3rd") and recall ("what do we know about Jane?", "who's allergic
  to anything?").
- **Any other subagent can query the store directly** via `Bash` — no need to route
  through the `profile` subagent for a simple read. This keeps lookups frictionless:
  meal-planner checks it for dietary restrictions, scheduler for birthdays/
  anniversaries, kids-memory for Ruth/Claire's structured facts, todoist for
  gift-relevant preferences. Only the `profile` subagent should ever write to it, to
  avoid duplicate person rows or inconsistent fact keys accumulating.
- Ruth and Claire get `profile_people` rows too (birthday, allergies, sizes, school);
  `kids-memory` keeps owning anecdotes/stories and Drive sync separately — the two
  systems are complementary, not merged.

```
python scripts/profile_store.py person add --name "Jane Doe" --relationship spouse --birthday 1990-03-14
python scripts/profile_store.py fact set --person-id <id> --key allergy --value "peanuts"
python scripts/profile_store.py person get --name "Jane Doe"
python scripts/profile_store.py global-fact set --key home_address --value "123 Main St"
```

No setup needed beyond what's already built — the schema applies itself on first use.

## Personal finance program (Phases 1–5)

A multi-agent personal-finance suite built on Monarch Money data — **all five phases are
built**. Four scoped subagents share a common working-state store (`scripts/finance_store.py`)
and the Monte Carlo retirement model. Full plan/history:
`docs/superpowers/specs/2026-07-22-personal-finance-agent-backlog.md`.

Monarch's **official** MCP (`mcp__claude_ai_Monarch_Money__*`) is currently paused by
Monarch. Until it's restored, these agents talk to Monarch through a **local, vendored MCP
server** instead: `vendor/monarch-mcp-server/` (the community
[`robcerda/monarch-mcp-server`](https://github.com/robcerda/monarch-mcp-server), built on
the actively-maintained `MonarchMoneyCommunity` fork), registered as `monarch` in
`.mcp.json`. Credentials never pass through Claude — auth is a one-time terminal step that
saves a session to the OS keyring:

```
cd vendor/monarch-mcp-server
python login_setup.py   # choose option 1: paste browser session cookies from app.monarch.com
```

All four subagents are **read-only against Monarch except `finance`** (the only one that
writes tags / rules / review status), default to **Sonnet**, and follow the same "derive
from data, be transparent about assumptions/gaps, never hand-wave a number" discipline.

### `finance` — transaction review & WHO tagging (Phase 1)

- Pulls transactions needing review (`get_transactions_needing_review`), skips anything
  still pending, and infers a **WHO tag** (the user's convention: `WHO - <person>` for one
  person, `Adults`/`Kids`/`Family` for mixed) from a learned merchant/account map
  (`finance_who_map`), account ownership, and the personal profile store.
- **Auto-applies** the tag only when confident, always merging with the transaction's
  existing tags (`set_transaction_tags` replaces the whole set, so trip/event tags are
  preserved deliberately). Ambiguous cases are surfaced with a short set of choices.
- **Never clears "needs review" on its own** — it tags, summarizes, and asks; only
  user-confirmed transactions get `mark_transaction_reviewed`. Proposes standing Monarch
  **rules** (merchant → WHO tag) once a pattern is confirmed, created only on explicit
  approval. Runs an on-demand **historical sweep** (resumable cursor in `finance_config`).
- Also answers factual data-backed questions (net worth, budget-vs-actual) directly.

### `finance-reporter` — spending summaries & annual review (Phases 2 & 4)

- Turns Monarch data into **weekly / monthly / annual** spending-summary reports — a brief
  Telegram message plus a self-contained HTML report (rendered by
  `scripts/finance_report.py`, `--period weekly|monthly|annual`) saved to a `Reports/`
  subfolder in Google Drive. Budget vs. actual, prior-period deltas, and spend-by-WHO.
- The **annual review** adds month-by-month + 3-year trends and an agent-authored
  narrative (executive summary, income/spending, net-worth, forward-looking), every claim
  citing a computed number.
- Scheduled tasks: `weekly-spending-summary`, `monthly-spending-summary`,
  `annual-financial-review` (the monthly/annual ones self-gate to the last Saturday).

### `retirement` — retirement modeling (Phase 3)

- Runs a **Monte Carlo retirement projection** (`scripts/retirement_model.py`, numpy) from
  Monarch-derived, user-overridable assumptions (stored in `finance_config` under
  `retirement_assumptions`) → success probability, p10/p50/p90 bands, ending-balance
  percentiles, and what-if scenarios.
- Produces a **deep, user-editable `.xlsx` workbook** (`scripts/retirement_workbook.py`) —
  live Excel formulas on an editable Assumptions tab, a Monte Carlo band tab, one tab per
  scenario — uploaded to a `Retirement/` Drive subfolder in place via `scripts/drive_upload.py`
  (Drive revision history = the archive).
- Owns the formal **"can we afford X"** what-ifs (retire earlier, a second home, a big
  renovation) and the scheduled `retirement-quarterly-review` (plan-vs-actual, last
  Saturday of Jan/Apr/Jul/Oct).

### `finance-advisor` — proactive recommendations (Phase 5)

- **On-demand, read-only, pure advice.** Turns real Monarch data (cash, debt + APRs,
  holdings/allocation incl. taxable unrealized losses, cashflow surplus, budget headroom)
  plus the retirement model's headroom into **grounded, ranked recommendations** — a
  grounded tier first (idle-cash-vs-invest, debt-payoff-vs-expected-return, tax-advantaged
  optimization HSA/401k/backdoor-Roth/529, tax-loss harvesting, insurance/estate gaps),
  then directional/speculative ideas (rental, business, big trip, new car) framed against
  what the model says is affordable.
- Ask it "what should we do with our money", "should we pay down the mortgage or invest",
  "any tax-advantaged opportunities we're missing", "any tax-loss harvesting", "insurance
  or estate gaps". Chat/Telegram output only — no Drive artifact.
- The facts Monarch can't provide (tax bracket, HSA eligibility, insurance/estate coverage,
  liquidity target, risk tolerance) live in a stored `advisor_profile` (`finance_config`) —
  asked once, persisted, never invented silently. **Follow-up not yet built:** fold an
  advisor section into the annual review report.

Backed by `finance_who_map` / `finance_tag_log` / `finance_rule_proposals` /
`finance_report_log` / `finance_config` and `scripts/finance_store.py`. No setup beyond the
Monarch login above and the Drive connector (already authorized); the retirement workbook's
Drive-upload CLI has its own one-time `python scripts/drive_upload.py auth` step (see
`docs/retirement-drive-setup.md`).

## Hy-Vee cart builder

Turn the Todoist shopping list into a **built (never placed)** Hy-Vee Aisles Online cart.
Say "build my Hy-Vee cart" (chat or Telegram) and the `hyvee` subagent reads the list,
resolves each item to a specific product, auto-adds the confident matches, asks about the
rest in one batch, verifies the cart, and hands back a review summary for you to finalize
and check out yourself. It **never** places an order.

- **Learns from your purchases.** Item→product matching is seeded from your Hy-Vee order
  history and improves every run: confidently-bought items auto-add; ambiguous ones are
  flagged for a single batched confirmation, and your accept/reject/substitute feedback
  tunes confidence over time (`hyvee_item_prefs`, `hyvee_feedback_log`).
- **Tie-breakers when there's no history match:** purchase history → your explicit brand
  preference → on sale → lower cost → Hy-Vee store brand as the safe default.
- **Structured shopping-list items.** The meal-planner (and manual adds) can attach
  `item:` / `brand:` / `size:` / `product_id:` / `qty:` / `note:` lines in a Todoist task's
  description so downstream matching is exact rather than guessed.
- **Two layers, cleanly split:** `scripts/hyvee_store.py` (zero-dep SQLite decision/learning
  store) and `scripts/hyvee/` (Playwright automation). When Hy-Vee changes their site,
  `python scripts/hyvee/diagnose.py check` reports exactly which selector/endpoint broke, and
  the fix is a one-line change in `scripts/hyvee/hyvee_web.py`.

One-time setup — put `HYVEE_USERNAME`/`HYVEE_PASSWORD` in `.env`, then:
```
pip install -r scripts/hyvee/requirements.txt && playwright install chromium
python scripts/hyvee/cart_ops.py sync-history
python scripts/hyvee_store.py history ingest --json <json_path from sync-history>
python scripts/hyvee_store.py seed --items "milk,eggs,bread,bananas,..."
```
Build-only, always: `cart_ops.py` has no checkout command by design.

## State store CLI (reference)

```
python scripts/state_store.py write --agent todoist \
  --task "add 'buy milk' to shopping list" \
  --summary "Created task 'buy milk' in project 'Shopping'." \
  --detail-json '{"task_id":"123","content":"buy milk","project":"Shopping"}'

python scripts/state_store.py query --agent todoist --limit 5
```

## End-to-end verification (Phase 1 Definition of Done)

Run this exact sequence — **Phase 1 is not done until step 7 works**:

1. Text the bot: **"add 'buy milk' to my shopping list"**
2. Orchestrator delegates to the `todoist` subagent.
3. Subagent creates the task in Todoist (creating the list if needed) and writes a row
   to the state store.
4. Orchestrator replies in the same Telegram chat.
5. Send a follow-up: **"what did I just add, and to which list?"**
6. Orchestrator re-delegates to the `todoist` subagent.
7. Subagent reads its own saved record and answers **with continuity** — not a generic
   restatement, not "I don't have that context."

Also confirm:
- `git status` shows no secrets tracked (drop a dummy secret in `.env` / `state/*.db` —
  it must not appear).
- Messaging from a non-allowlisted Telegram account does **not** reach the orchestrator.

## Out of scope for Phase 1

Email delivery, subagents beyond Todoist, containerization, Remote Control,
notification-formatting skills. (Cron/Task Scheduler and a custom local channel were
originally listed here too — both are now built; see **Scheduled tasks** above.)

## Backlog / Future Ideas

Not scheduled, not designed — just captured so they don't get lost. Each would get its
own brainstorm/spec before being built.

- [x] **Personal finance program — Phases 1–5 (all built)** — Monarch connectivity + WHO
      tagging (`finance`), weekly/monthly/annual spending reports (`finance-reporter`),
      Monte Carlo retirement modeling + editable workbook (`retirement`), and the proactive
      recommendations engine (`finance-advisor`). Official Monarch MCP is paused, so this
      uses a vendored community MCP server. See **Personal finance program** above and
      `docs/superpowers/specs/2026-07-22-personal-finance-agent-backlog.md`.
      - [ ] **Remaining follow-up:** fold a `finance-advisor` recommendations section into the
            annual review report (`finance-reporter` annual mode) so the proactive layer also
            lands once a year, not just on-demand.
      - [ ] **Retirement model refinement:** derive asset allocation from real holdings
            (currently an 85/15 assumption) and replace the placeholder Social Security
            estimate with a real one — both are editable levers today, this is a
            "make the numbers trustworthy" pass. See the Phase 3 backlog note.
- [x] **`scripts/finance_report.py` — annual report support** — `--period annual` now renders
      `by_month`, `trends`, and agent-authored `narrative` sections (built with Phase 4).
- [x] **Shopping cart builder — Phase 1 (Hy-Vee)** — build (never place) a Hy-Vee Aisles
      Online cart from the Todoist shopping list, resolving each item to a specific product
      from purchase history + learned preferences, with a feedback loop that sharpens matching
      every run. Built: `hyvee` subagent, see **Hy-Vee cart builder** above.
- [ ] **Shopping cart builder — Phase 2 (Target + cross-store optimization)** — add a second
      cart builder for Target (same build-only, learn-from-history model as Hy-Vee), then
      optimize *where* each item is bought: compare price/availability across Hy-Vee and Target
      and route each item to the cheaper (or preferred) store, staging a cart at each. Still
      stops short of checkout — a human reviews and places both orders.
- [x] **Weather reminders** — proactive nudges ahead of incoming weather (shovel snow, bring
      in cushions, etc.). Built: `weather-reminders` subagent, see **Weather reminders agent**
      above.
- [ ] **Shopping assistant (deal-watching)** — watches for deals on non-urgent wanted items
      across stores, Craigslist, Facebook Marketplace, etc.
- [x] **Fitness coach agent (Phases 1-3)** — plans weekly workouts (3-4 runs, 2 strength
      incl. one lower-body, 1 Peloton) by reusing existing COROS Training Hub library
      workouts, gated on user approval, then writes them to both Coros and the
      "Running" Google Calendar. Built: `fitness` subagent, see **Fitness agent**
      above. Weekly trigger: `weekly-fitness-plan` scheduled task, Sundays 6pm.
      - [ ] **Custom workout creation** — the "Create Workouts" interval builder isn't
            automated yet; the agent flags a gap and asks the user to build it manually
            in Coros rather than guessing at a structure.
      - [ ] **History-informed planning** — `fitness_weekly_plans.completion_status` is
            tracked but nothing yet reads recent weeks' adherence to adjust the next
            plan.
      - See `docs/superpowers/specs/2026-08-20-fitness-agent-coros-login-design.md`
        and the two follow-on specs in the same directory for the full phased design,
        including why drag-and-drop scheduling was abandoned for a reverse-engineered
        API call.
- [ ] **Google Chat bridge** — a second chat channel (alongside Telegram) so the assistant
      is reachable from Google Chat too.
- [ ] **Grocery list from pantry/fridge photos** — snap a picture of what's on hand and
      generate the grocery list from what's actually missing.
- [x] **Smart home agent (Phases 1-3)** — `.claude/agents/home-assistant.md` handles
      ad-hoc control/query (lights, climate, media, covers, locks) via the hosted Home
      Assistant connector, plus automation/script/scene/helper/dashboard authoring and
      history/log debugging via the local `ha` MCP server (see the "Home Assistant
      agent" section above).
      - [ ] **Phase 4 — proactive monitoring:** watch device/sensor state (e.g. garage
            door left open, entity unavailable) via the existing `scheduler` engine,
            with a presence-based dedup log (`ha_alerts` table / `scripts/ha_store.py`,
            not yet built) so an alert clears and can re-fire once its condition
            resolves and recurs — same silent-when-healthy, propose-then-confirm
            pattern as `weather-reminders`/`home-maintenance`.
      - [ ] **Phase 5 — ambient HA context for other agents:** document (no new
            subsystem) that other subagents can be granted a read-only `mcp__ha__*` or
            `GetLiveContext` tool directly, or delegate a one-off question to
            `home-assistant`, to enable combined routines like "goodnight" or "leaving
            for school" that blend the calendar + HA state.
      - See `docs/superpowers/specs/2026-08-19-home-assistant-integration-design.md`
        for the full phased design.
- [ ] **Email triage / inbox digest** — the Gmail MCP server is also already connected but
      unused. A daily/weekly digest of what needs action (school notices, bills, appointment
      confirmations), same proactive shape as the heartbeat skill above.
- [ ] **Birthday/gift-reminder agent** — the data layer now exists (`profile_people.birthday`
      via the **Personal profile** section above, plus the Todoist subagent's per-person Gift
      Ideas sub-lists); still needs a proactive scheduled check that cross-references upcoming
      birthdays against what's already on that person's gift list.
- [x] **Home maintenance agent** — same shape as the lawn & garden agent, but indoor: HVAC
      filters, smoke detector batteries, water softener salt, garage clean-outs, seasonal
      chores. Built: `home-maintenance` subagent, see **Home maintenance agent** above.
- [ ] **Kids memory keeper — photo/audio support** — extend beyond text (transcribed voice
      notes, photos) now that the schema/Drive pipeline is in place (`media_type` column
      already anticipates this). Capture, recall/chat, and proactive monthly/weekly
      scheduled prompts are all built — see the **Kids memory keeper** section above.

### Someday / needs more refinement

Lower priority, idea-only — not enough shape yet to even brainstorm.

- Trip/travel planning (packing lists, itineraries tied to the calendar)
- Family "newsletter" recap — a personal weekly digest, possibly reusing the existing
  `/newsletter` skill's pattern (currently built for technical/business newsletters)
