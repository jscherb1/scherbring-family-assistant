# Personal AI Assistant (Telegram → bridge → agent worker → subagents)

A personal multi-agent assistant orchestrated through Claude Code, running locally on
Linux (built on WSL2 Ubuntu 24.04, run by systemd). One agent (run by the agent worker) is the single point of contact; it delegates to scoped
subagents. Phase 1 proves the core loop end-to-end with one real subagent (**Todoist**)
and a shared state store that gives follow-up questions continuity.

```
You (Telegram) → telegram_bridge.py → inbox (SQLite) → agent_worker.py (Claude Agent SDK)
                                                              │ delegates via Task tool
                                                              ▼
                                                     Todoist subagent  ── Todoist MCP (tasks/lists)
                                                              │
                                                              ▼ writes/reads
                                                     Shared state store (SQLite)  → reply sent by the bridge
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
| `.claude/agents/chores.md` | Chores subagent — kid chore chart (chores, dollar balances, streaks, payouts), the weekly payday summary and the optional afternoon open-chores check |
| `.claude/agents/myhub.md` | My Hub subagent: answers "what needs my attention?" and keeps the hub's calendar/Todoist snapshot fresh (scheduled morning + evening) |
| `.claude/agents/dayrating.md` | Day-rating subagent: daily 1-10 rating with a note, the nightly nudge, and the weekly insights report (stats plus an AI-written story) |
| `.claude/agents/journal.md` | Journal subagent: the 8:30 PM nightly check-in (day rating + yes/no journal questions + open daily habits in one message), recording the reply, managing questions, and "what does X do to my HRV/sleep/mood" |
| `.claude/agents/wellness.md` | Wellness subagent (read-only, Whoop-style): today's recovery, sleep and vitals against your normal, trends over time, and what drives the day rating / HRV / sleep / resting HR |
| `.claude/agents/ha-overview.md` | Home Assistant status/health subagent — same data as the dashboard's Home > Home Assistant pages: what needs attention, recommendations, thermostat/door/garage state, and a few allow-listed changes (thermostats, kid door alerts, Christmas lights) |
| `.claude/agents/golf.md` | Golf subagent: 18-hole rounds and handicap (mirrors the Handicap Google Sheet), logging rounds, reading scorecard photos sent in chat |
| `.claude/agents/habits.md` | Habits subagent — habit/streak tracker (daily or weekly goals, no-goal counters), logging, streaks, and the daily at-risk nudge |
| `.claude/agents/home-maintenance.md` | Home-maintenance subagent — recurring indoor maintenance items (HVAC filters, smoke detector batteries, etc.), completion log, and the weekly proactive due-check |
| `.claude/agents/home-assistant.md` | Home-assistant subagent (Phases 1-3) — ad-hoc smart-home control/query via the hosted Home Assistant connector plus a local `ha` MCP server (vendor/ha-mcp) for automation/script/scene/helper/dashboard authoring and history/log debugging; proactive monitoring is a later phase |
| `vendor/ha-mcp/` | Vendored [`homeassistant-ai/ha-mcp`](https://github.com/homeassistant-ai/ha-mcp) (PyPI package, `.venv`) — runs as a loopback-only local HTTP server (`systemd/assistant-ha-mcp.service`) that Claude connects to via the `ha` entry in `.mcp.json` |
| `.claude/agents/fitness.md` | Fitness subagent (Phases 1-3) — plans weekly workouts by reusing the COROS Training Hub library, gated on approval, writes to Coros + the "Running" Google Calendar |
| `scripts/fitness_store.py` | Zero-dep CLI for fitness data (workout-library cache sync/list, weekly-plan add/list/update sub-commands) |
| `scripts/fitness/` | Playwright + API browser layer for COROS Training Hub — `login_test.py` (session), `library_sync.py` (Workout Library scrape), `workout_sync.py` (logs the Workouts habit from recorded COROS activities; daily `workouts-habit-sync` task), `schedule_ops.py` (list-week / add-existing-workout / remove-workout, via the real Coros API, not drag-and-drop), `coros_web.py` (selector/URL/endpoint constants), `coros_session.py` (shared helpers), `coros_mcp.py` (OAuth client for COROS's official MCP server: sleep, steps, stress; one-time sign-in via `auth-url` / `auth-finish`, token in `state/coros_mcp_token.json`) |
| `.claude/agents/finance.md` | Finance subagent (Phase 1) — Monarch transaction review + WHO tagging, rule proposals, historical sweeps |
| `.claude/agents/finance-reporter.md` | Finance-reporter subagent (Phases 2 & 4) — weekly/monthly/annual spending-summary HTML reports to Drive + Telegram brief |
| `.claude/agents/retirement.md` | Retirement subagent (Phase 3) — Monte Carlo projection + editable `.xlsx` modeling workbook to Drive; owns "can we afford X" what-ifs |
| `.claude/agents/finance-advisor.md` | Finance-advisor subagent (Phase 5) — on-demand, read-only proactive recommendations engine (idle cash, debt, tax-advantaged, tax-loss, insurance/estate + directional ideas) |
| `scripts/money.py` | Shared Money service over the local Monarch snapshot (budget vs plan with red/yellow/green, spending vs normal, savings rate, runway, net worth, review queue, reconciling the tagging log). Used by `money_store.py` and the `/finance` dashboard pages |
| `scripts/money_store.py` | Zero-dep CLI over `money.py` for the `finance` agent (`overview`, `budget`, `spending`, `review`, `sync-status`, `config`) |
| `scripts/money_sync.py` | Pulls Monarch into the snapshot tables (runs with the vendored Monarch venv; `assistant-money-sync.timer`) |
| `scripts/finance_store.py` | Zero-dep CLI for finance working-state (who-map / tagging log / rule proposals / report log / config sub-commands) |
| `scripts/finance_report.py` | Renders self-contained HTML spending reports (`--period weekly|monthly|annual`) |
| `scripts/retirement.py` | Shared retirement service: classifies Monarch-snapshot accounts into buckets (pretax/Roth/HSA/taxable/cash/kids), derives balance, allocation and savings, resolves assumptions, runs the model, builds the workbook payload |
| `scripts/retirement_store.py` | Retirement CLI for the `retirement` and `finance-advisor` agents (`inputs`, `accounts`, `securities`, `config`, `run`, `workbook`) |
| `scripts/retirement_model.py` | Monte Carlo retirement engine (numpy) — capital-market-priced allocation with a glide path, success probability ± noise, percentile bands in nominal and today's dollars, base + what-if scenarios |
| `scripts/retirement_data/cma.json` | Capital-market assumptions (forward-looking 10-15 yr returns, volatilities, correlations; refreshed yearly) |
| `scripts/retirement_tax.py` | Federal + Minnesota income tax, Social Security taxation and claiming rules for the retirement engine (numpy, vectorized) |
| `scripts/retirement_data/tax_2026.json` | Tax tables, RMD divisors, Social Security bend points and contribution limits (refreshed yearly) |
| `scripts/retirement_workbook.py` | Builds the user-editable `.xlsx` retirement modeling workbook (xlsxwriter) |
| `scripts/drive_upload.py` | Path-based Google Drive upload/in-place-update CLI (own OAuth token) for the binary `.xlsx` workbook |
| `.mcp.json` | Project MCP config (Todoist HTTP/OAuth, local `ha`, vendored `monarch`). Gitignored. See `.mcp.json.example`. |
| `state/schema.sql` | SQLite schema for `agent_results`, `recipes`, `scheduled_tasks`, `scheduled_task_runs`, `kid_memories`, `kid_memory_triggers`, `lawn_garden_plants`, `lawn_garden_products`, `lawn_garden_program`, `lawn_garden_treatments`, `lawn_garden_issues`, `lawn_garden_config`, `profile_people`, `profile_people_facts`, `profile_facts`, `weather_config`, `weather_alerts`, `home_maintenance_items`, `home_maintenance_completions`, `hyvee_purchase_history`, `hyvee_item_prefs`, `hyvee_feedback_log`, `hyvee_cart_runs`, `finance_who_map`, `finance_tag_log`, `finance_rule_proposals`, `finance_report_log`, `finance_config`, `money_sync_runs`, `money_accounts`, `money_transactions`, `money_budget_totals`, `money_budget_lines`, `money_networth_history`, `money_recurring`, `money_holdings`, `money_config`, `fitness_workout_library`, `fitness_weekly_plans`, `habits`, `habit_logs`, `habit_config`, `golf_rounds`, `golf_courses`, `golf_config`, `day_ratings`, `insight_daily`, `insight_reports`, `journal_questions`, `journal_answers`, `calendar_load`, `hub_items`, `system_health`, `chore_kids`, `chores`, `chore_assignments`, `chore_completions`, `chore_ledger`, `chore_config`, `ha_health_runs` |
| `state/agent_results.db` | The state store (auto-created; gitignored — holds personal data) |
| `scripts/state_store.py` | Zero-dep CLI the subagents call to write/read continuity results |
| `scripts/recipes.py` | Shared recipe service (rules, add/edit/delete, cooking history, feedback) used by both the CLI and the dashboard |
| `scripts/recipes_store.py` | Zero-dep CLI for the recipe library (list/get/add/update/delete/feedback/mark-cooked), a thin wrapper over `recipes.py` |
| `scripts/scheduler_store.py` | Zero-dep CLI for the scheduled-tasks registry (add/list/enable/disable/delete/due/log-run); called by `scheduler_dispatch.py` to find and dispatch due tasks |
| `scripts/scheduler_dispatch.py` | Zero-token external poller — writes a heartbeat tick, checks for due tasks, and dispatches each via `claude --print` as a one-shot subprocess. Exits silently when nothing is due. |
| `systemd/` | systemd **user** unit templates (`@REPO_ROOT@` placeholder): Telegram bridge, agent worker, scheduler, watchdog, ha-mcp, dashboard, credential check, backup |
| `scripts/install_systemd.sh` | Installs the units into `~/.config/systemd/user/` and optionally enables named ones |
| `scripts/setup_wsl.sh` | Builds `.venv`, installs Python deps + Playwright Chromium, the pinned `ha-mcp` and Monarch servers |
| `scripts/paths.py` | Shared locations and secrets loading (`~/.config/scherbring-assistant/.env`); every script goes through this, nothing hardcodes a home directory |
| `scripts/telegram_bridge.py` | Telegram bridge: polls Telegram into a durable inbox (`state/inbox.db`) and delivers replies from the outbox with retry |
| `scripts/agent_worker.py` | Agent worker: runs Claude turns for inbox messages via the Agent SDK, resumes its session after a restart |
| `scripts/agent_ctl.py` | Operate the bridge and worker: `status`, `tail`, `say`, `pause`/`resume`, `sessions`, `drill` |
| `scripts/run_watchdog.sh` | One watchdog pass (scheduler health + log pruning); run every 2 minutes by `assistant-watchdog.timer` |
| `scripts/watchdog_scheduler_health.py` | Detects a stalled scheduler dispatcher (via the heartbeat file), restarts the timer, and sends a direct Telegram alert |
| `scripts/scheduler_run_now.py` | Runs one scheduled task immediately through the real dispatch path, for testing |
| `scripts/credential_check.py` | Daily real-request check that messages on Telegram only when a Google Drive or Monarch login is really down (Hy-Vee is owned by `hyvee_connection.py`) |
| `scripts/backup.py` | Daily backup: SQLite snapshot, Claude memory notes, age-encrypted secrets; keeps 14 daily + 8 weekly |
| `scripts/monarch_result.py` | Read-only queries over large saved Monarch results, so headless runs need no `python -c` or `jq` |
| `scripts/kid_memories_store.py` | Zero-dep CLI for kid memories (add/update/get/list/mark-drive-synced/mark-drive-failed/add-trigger/triggers) |
| `scripts/lawn_garden_store.py` | Zero-dep CLI for lawn & garden data (plant/product/program/treatment/issue/config sub-commands) |
| `scripts/profile_store.py` | Zero-dep CLI for the personal profile store (person/fact/global-fact sub-commands) — any subagent can call it directly |
| `scripts/weather_store.py` | Zero-dep CLI for weather-reminders data (config/alert sub-commands) |
| `scripts/chores.py` | Shared chore-chart service (completion rules, money ledger, badges; streaks via `habits.habit_status`). The single code path used by `chores_store.py`, the `/chores` dashboard section and the `/kids` page |
| `scripts/chores_store.py` | Zero-dep CLI over `chores.py` for the chores subagent (kid/chore/done/undo/status/payout/config sub-commands) |
| `scripts/myhub.py` | Shared My Hub service: runs every provider in `scripts/hub_providers/`, merges their needs-attention items into one ranked list, stores the calendar/task snapshot. Used by `myhub_store.py` and the dashboard `/me` page |
| `scripts/myhub_store.py` | Zero-dep CLI over `myhub.py` for the myhub subagent (summary / attention / dismiss / restore / notify-check / snapshot set / snapshot status) |
| `scripts/system_health.py` | Shared integration-health service: one latest check result per system (COROS health + training hub, Open-Meteo, Drive, Monarch, Hy-Vee, plus Todoist, Calendar, Gmail, Home Assistant recorded by an agent). One failure is `warn`, two in a row `down`. Feeds the hub Systems tile and attention strip |
| `scripts/reauth.py` | Remote re-auth service: Google Drive two-step paste sign-in (consent link, then paste the `localhost/callback` address), so an expired Drive login can be refreshed from any device. Shared by `reauth_store.py` (Telegram, via the `myhub` agent) and the dashboard Reconnect page |
| `scripts/reauth_store.py` | Thin CLI over `reauth.py` (start / complete / verify) |
| `scripts/hyvee/hyvee_connection.py` | The one Hy-Vee connection: a single three-outcome check (ok / expired / unknown, always with a reason), the post-login hook every sign-in calls, and a keep-alive that re-saves the session on each pass. Run every 2 h via `system_health`; CLI `check` / `status` |
| `scripts/system_health_store.py` | Zero-dep CLI over `system_health.py` (status / check / record); sends one Telegram alert when a system goes down and one on recovery |
| `scripts/hub_providers/` | One module per hub area (habits, mood, body, schedule, weather, home, chores, system, insights). Drop in a module and its card and needs-attention items appear on the hub |
| `scripts/dayrating.py` | Shared day-rating service (1-10 score + note per day, status, nudge check). The single code path used by `dayrating_store.py` and the dashboard |
| `scripts/dayrating_store.py` | Zero-dep CLI over `dayrating.py` and `insights.py` for the dayrating subagent (rate/show/list/status/nudge, insights refresh/stats/save) |
| `scripts/calendar_load.py` | Per-day calendar load rules and storage (events, hours booked, shift/travel/kid flags). Events come from the dayrating agent's Calendar connector via `dayrating_store.py calendar sync` |
| `scripts/insights.py` | Insights engine: auto-discovers sources in `scripts/insight_sources/`, stores daily metrics, ranks what moves the rating (Spearman, with/without comparisons, permutation p-values), stores AI narratives |
| `scripts/insight_sources/` | One module per data source (`habits`, `calendar_day`, `weather` via free Open-Meteo, `coros_wellness` for HRV (and its normal range) / resting HR / fatigue / load (incl. acute and chronic load and COROS's recommended 7-day range), `coros_health` for sleep / steps / stress from the official COROS MCP server). Drop a new module here and it flows into the analysis |
| `scripts/journal.py` | Shared journal + nightly check-in service (yes/no questions and answers, habit import, check-in pending/message/record across rating, journal and habits, yes-vs-no impact). The single code path used by `journal_store.py` and the Wellness check-in card / Journal page |
| `scripts/journal_store.py` | Zero-dep CLI over `journal.py` for the journal subagent (checkin message/pending/record, answer, day, history, impact, question list/add/update/import-habit) |
| `scripts/wellness.py` | Shared wellness service (read-only): metric timing classes and point status, readiness, vitals vs personal baseline, trend series and bands, heatmap, drivers, scatter. The single code path used by `wellness_store.py` and the `/wellness` dashboard section |
| `scripts/wellness_store.py` | Zero-dep CLI over `wellness.py` for the wellness subagent (today / metric / trends / drivers / scatter / metrics) |
| `scripts/ha.py` | Shared Home Assistant board + health service (REST + minimal websocket client, board definition, health rules, write allow-list; alarm/garage/locks read-only). The single code path used by `ha_store.py` and the Home > Home Assistant dashboard pages |
| `scripts/ha_store.py` | Zero-dep CLI over `ha.py` for the ha-overview subagent (`health`, `board`, `set-temp`, `set-mode`, `toggle`) |
| `scripts/golf.py` | Shared golf service (differentials, best-8-of-last-20 x 0.96 handicap, round logging/undo, saved courses). The single code path used by both `golf_store.py` and the dashboard |
| `scripts/golf_store.py` | Zero-dep CLI over `golf.py` for the golf subagent (round/course/handicap/import/sync sub-commands) |
| `scripts/golf_sheet.py` | Google Sheets side of golf: read history, append new rounds to the "Handicap - 18 holes only" tab (best-effort mirror) |
| `scripts/habits.py` | Shared habit-tracker service (streak rules, logging, at-risk). The single code path used by both `habits_store.py` and the dashboard |
| `scripts/habits_store.py` | Zero-dep CLI over `habits.py` for the habits subagent (habit/log/at-risk/config sub-commands) |
| `scripts/home_maintenance_store.py` | Zero-dep CLI for home maintenance data (item/completion/due sub-commands) |
| `.claude/agents/hyvee.md` | Hy-Vee cart-builder subagent — resolves the Todoist shopping list to specific products and builds (never places) a Hy-Vee Aisles Online cart |
| `scripts/hyvee_store.py` | Zero-dep CLI for the cart-builder decision store (history / prefs / feedback / seed / resolve / rank / run sub-commands) |
| `scripts/hyvee/` | Playwright browser layer — `cart_ops.py` (login, purchase-history sync, product search, add-to-cart, cart verify), `diagnose.py` (site-change maintenance toolkit), `hyvee_web.py` (selector/URL/endpoint constants), `hyvee_session.py` (shared helpers) |
| `.env.example` | Template listing the keys for `~/.config/scherbring-assistant/.env` (the secrets file, outside the repo). Hy-Vee, COROS, Home Assistant and Telegram alert settings go there. |

> **Location matters:** keep this project on the Linux filesystem (not under `/mnt/c`) and outside
> any corporate-synced folder. The personal task DB and any tokens must not sync to a cloud
> tenant. Credentials live in `~/.config/scherbring-assistant/`, outside the repo. Back up with the
> daily backup job (`scripts/backup.py`, see `docs/OPERATIONS.md`) plus a **private** git remote.

## Setup

### A. Manual, one-time (you — needs an interactive `claude` session + a browser)

0. **Build the environment** (once, from the repo root): `scripts/setup_wsl.sh`. It creates
   `.venv`, installs the Python dependencies and Playwright's Chromium, and installs the pinned
   `ha-mcp` and Monarch servers under `vendor/`. Then create the secrets file, which lives
   **outside the repo**: `~/.config/scherbring-assistant/.env` (directory mode 700, file mode
   600). Use `.env.example` as the list of keys. Every script reads it through `scripts/paths.py`.
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
5. **Telegram bot** — create a bot with [@BotFather](https://t.me/BotFather) and add its token to
   `~/.config/scherbring-assistant/.env` as `TELEGRAM_BOT_TOKEN` (use `TELEGRAM_TEST_BOT_TOKEN` plus
   `BRIDGE_TOKEN_ENV=TELEGRAM_TEST_BOT_TOKEN` to try it on a separate bot first). Then allowlist yourself:
   set `BRIDGE_ALLOWED_CHAT_IDS` to your Telegram chat id (comma-separated for several). While it is empty
   the bridge is in pairing mode: it serves nobody and replies to any sender with their chat id, so message
   the bot once, copy the id from the reply, put it in `.env`, and restart `assistant-bridge`. Messages from
   any chat id not on the list never reach the agent. Do **not** install the Telegram Claude plugin: it polls
   the same bot token and blocks the bridge.

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

## Launch (bridge + worker)

Two systemd user units serve Telegram. `assistant-bridge.service` (`scripts/telegram_bridge.py`) owns
the Telegram connection: it saves every message to `state/inbox.db` before acknowledging it and sends
replies from an outbox. `assistant-agent-worker.service` (`scripts/agent_worker.py`) runs the Claude turns
with `--permission-mode auto` and resumes its session by id after a restart, so either can be restarted
without losing a message. Both start from systemd (see below); message your bot to use it.
`scripts/agent_ctl.py status` shows whether both are up; `docs/OPERATIONS.md` ("Telegram bridge") has the
runbook.

## Always-on: systemd user units

Everything runs as **systemd user units**, templated in `systemd/` and installed with
`scripts/install_systemd.sh` (which fills in this checkout's path):

| Unit | What it does |
|---|---|
| `assistant-bridge.service` | Telegram bridge: polls Telegram into the inbox, delivers replies. Restarts on failure. |
| `assistant-agent-worker.service` | Agent worker: runs Claude turns for inbox messages (systemd watchdog 30 s). |
| `assistant-scheduler.timer` / `.service` | Scheduled-task dispatcher, every 2 minutes. |
| `assistant-watchdog.timer` / `.service` | Scheduler-health check, every 2 minutes, plus log pruning. |
| `assistant-ha-mcp.service` | Local Home Assistant MCP server on 127.0.0.1:8086. |
| `assistant-system-health.timer` / `.service` | Every 2 h: script-side integration health checks (`system_health_store.py check`); no model tokens. Enable with `scripts/install_systemd.sh assistant-system-health.timer` |
| `assistant-credential-check.timer` / `.service` | Daily 09:30 warning before Drive or Monarch logins expire. |
| `assistant-backup.timer` / `.service` | Daily 03:15 backup of the database, memory notes and encrypted secrets. |
| `assistant-dashboard.service` | Optional Scherbring Family Assistant web dashboard (FastAPI; system status, agent, scheduled tasks, recipes). Binds 127.0.0.1 unless a drop-in sets another address. |

First-time install on a fresh machine:

```
scripts/setup_wsl.sh                    # venv, Python deps, Playwright, ha-mcp, Monarch server
sudo loginctl enable-linger "$USER"     # user units run without a login session
scripts/install_systemd.sh assistant-bridge.service assistant-agent-worker.service \
    assistant-scheduler.timer assistant-watchdog.timer assistant-ha-mcp.service assistant-credential-check.timer \
    assistant-backup.timer
```

**On WSL2** the VM must also stay up on its own: `/etc/wsl.conf` needs `[boot] systemd=true`,
`%UserProfile%\.wslconfig` needs `vmIdleTimeout=-1` under `[wsl2]`, and one Windows startup task
keeps the VM alive (`wsl.exe -d Ubuntu-24.04 -u <user> -- sleep infinity`).
`docs/OPERATIONS.md` has the full day-to-day runbook: status, logs, restart, backup, restore.

## Scheduled tasks (proactive, recurring prompts)

Lets you ask the assistant to do something on a schedule — "every Sunday at 9am,
plan next week's dinners and post it" — and have the result posted into this same
Telegram chat. Adding a new scheduled task is just a conversation with the `scheduler`
subagent (or a row in the registry via `scheduler_store.py`) — it never touches systemd.

**Architecture:** a systemd user timer (`assistant-scheduler.timer`) runs
`scripts/scheduler_dispatch.py` every 2 minutes. That script:
1. Writes a heartbeat tick to `state/scheduler_loop_state.json` (monitors health).
2. Runs `scheduler_store.py due` to check the SQLite registry for anything due. Cron is
   evaluated in the machine's local time zone, so `/etc/localtime` must be right.
3. **If nothing is due — exits immediately. Zero Claude API calls, zero tokens.**
4. If tasks are due — runs each as a one-shot `claude --print --model sonnet` subprocess (fresh
   context, 300 s limit). The agent wraps its reply in `<<<TELEGRAM_SEND>>>` markers and the
   dispatcher delivers it through the Telegram Bot API.
5. The run is read from `--output-format json` (falling back to raw stdout and the debug-log denial
   count if that can't be parsed). It is marked `failed`, and a direct Telegram alert goes out (no
   Claude involved), if it exits non-zero, the JSON result is an error, or the message fails to
   deliver. If any commands were denied, the run is `degraded` (no alert; the summary names the
   denied commands) only when a marked message was delivered and that message does not report the
   task itself failing (for example "sync failed", "did not complete"); a silent run with denials,
   or one whose delivered message reports a failure, is `failed`. Without denials, a reply that
   says it did not complete is not detected, so read the message. Missed runs after downtime collapse
   into one catch-up run.

A separate watchdog (`scripts/watchdog_scheduler_health.py`, run every 2 minutes by
`assistant-watchdog.timer`) verifies the heartbeat is ticking and restarts the timer if it goes
stale, staying quiet if the machine was asleep.

*(History: v1 used a hand-written local MCP channel server requiring
`--dangerously-load-development-channels`, which caused a multi-hour outage from
unautomatable interactive confirmation dialogs. v2 used an in-session `CronCreate`
poll loop that consumed ~720 idle Claude turns/day even when nothing was scheduled.
The current external-dispatch approach eliminates both problems.)*

One-time setup:

1. **Install and start the timers** (see **Always-on** above):
   `scripts/install_systemd.sh assistant-scheduler.timer assistant-watchdog.timer`
2. **Set the alert chat ID** the watchdogs use for direct Telegram alerts: add
   `TELEGRAM_ALERT_CHAT_ID=<your chat_id>` to `~/.config/scherbring-assistant/.env`.
3. Try it: message the bot "every day at [a couple minutes from now], say hello" and
   confirm the scheduled reply arrives.

To run one task right now through the real dispatch path (for testing; it does not
advance the task's schedule): `python scripts/scheduler_run_now.py --name <task>`
(`--list` shows the names; `--ignore-gates` overrides a task's own date check).

## Kids memory keeper

Tell the assistant (in chat, or via Telegram) a quick memory, anecdote, or note
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

## Golf agent

Tracks 18-hole rounds and the handicap from Justin's Google Sheet ("Justin's Golf Handicap Calculator",
tab "Handicap - 18 holes only"). The `golf` subagent and the dashboard's **Me > Golf** pages run the **same
code** (`scripts/golf.py`) against the same tables (`golf_rounds`, `golf_courses`, `golf_config`).

- **Handicap rule** (copied from the sheet): differential = (score - rating) x 113 / slope; take the last
  20 rounds by date, average the 8 lowest differentials, x 0.96, one decimal.
- **Source of truth is the local DB.** History was loaded once from the sheet (`golf_store.py import`).
  New rounds are appended to the sheet by `scripts/golf_sheet.py` (writes date/course/rating/slope/score and
  copies the formula columns F-I down). A failed mirror never blocks logging; `golf_store.py sync` (or the
  dashboard's "Sync to sheet") retries. Deleting a round does not delete its sheet row.
- **Scorecard photos:** send a photo in chat; the agent reads it from `state/attachments/`, replies with the
  extracted round and the handicap change, and logs only after you say yes. Hole-by-hole scores are not stored.
- Needs the **Google Sheets API enabled** on the same Cloud project as Drive (done 2026-10-24; the existing Drive login works;
  the `drive` scope covers Sheets). See `docs/TODO.md`.
- No scheduled task.

## Habits agent

A habit/streak tracker so there is one place for the things you want to keep up (or just count),
with no separate app. The `habits` subagent and the dashboard's **Habits** page run the **same
code** (`scripts/habits.py`) against the same tables (`habits`, `habit_logs`, `habit_config`), so a
habit logged in Telegram shows on the page instantly and a tap on the page is visible to the agent.

- **Habits** are daily or weekly, with an optional goal per period ("1×/day", "3×/week") and an
  optional owner (Justin, Caroline, Kids, Family; none = shared). A habit with **no goal** is only
  counted (total, this week, last 30 days, average gap).
- **Streaks**: consecutive periods that met the goal (weeks run Monday-Sunday). An unmet period
  still in progress doesn't break the streak; when a period ends unmet the streak resets and a new
  one starts. Current and longest streak are both shown.
- **Calendars**: every card flips (📅 Calendar) to show two months of the days that habit was done;
  `/habits/analysis` shows habits-done-per-day across all habits plus a per-habit rollup. Agent twin:
  `habits_store.py calendar|analysis`.
- **Logging** is one tap (dashboard "Done") or one sentence ("I stretched", "ran on Tuesday"); past
  dates are allowed, future dates are not, and Undo removes the last entry. Habits are paused, never deleted.
- **Daily at-risk nudge** (scheduled, silent when nothing is at risk): one evening message listing
  habits whose streak will break unless done. Reply "done <habit>" to log. Configurable: per habit
  (`--remind 0|1`) and globally (`config set --key nudge_enabled --value 0|1`); the time of day is the
  task's cron, changed through the `scheduler` subagent.

One-time setup: register the nudge —
```
python scripts/scheduler_store.py add --name "habits-daily-nudge" \
  --prompt "Delegate to the habits subagent to run the daily at-risk check." \
  --cron "0 19 * * *" --target-chat-id "<your chat_id>"
```

## Wellness dashboard

A Whoop-style view of everything the assistant already collects (COROS sleep, HRV, resting HR, load, steps, stress,
the day rating, habits). The `wellness` subagent and the dashboard's **Wellness** section run the **same code**
(`scripts/wellness.py`) over the same tables (`insight_daily`, `day_ratings`, habits). It writes nothing and needs no
scheduled task of its own: data arrives through the insights sources (30-minute timer for COROS sleep/activity,
weather and habits; the weekly `dayrating-weekly-insights` run for COROS HRV and load).

- **Timing**: each metric has a class (overnight, filed on the wake-up date; daytime, complete only once the day ends;
  evening, the day rating; snapshot, COROS recovery). Every daily point is final, partial, pending (not synced yet) or
  missing. Charts mark pending days with a dashed stub instead of a zero, averages and baselines use final days only,
  and the Today page shows yesterday's rating until today's is given in the evening.
- **Today** (`/wellness`): recovery / sleep / 7-day load rings (COROS recovery when read today, else an estimate from
  HRV, resting HR and sleep score against your baseline), last night's sleep and stages, vitals against your normal
  (mean ±1 SD over the prior 30 days), the day rating, habits and source freshness.
- **Trends** (`/wellness/trends`, 30/90/180 days): sleep hours, window, score and stages; HRV in its normal band;
  resting HR; training load with acute/chronic load and the load ratio zones; weekly steps and exercise; mood and
  rating by weekday; a 6-month calendar heatmap of any metric.
- **Drivers** (`/wellness/drivers`): what moves the day rating, HRV, sleep score or resting HR (the insights engine's
  findings, `insights.analyze` / `analyze_against`), a two-metric scatter, and the latest written story.
- Charts are server-rendered SVG (`dashboard/charts.py`); `dashboard/static/wellness.js` adds hover tooltips, a
  crosshair shared across charts, and tap-to-pin readouts on phones.

## Nightly check-in and journal

One evening message gathers everything the assistant needs from the user that day. The `journal` subagent and the
Wellness section's check-in card (Today) and **Journal** page run the **same code** (`scripts/journal.py`) against
`journal_questions`, `journal_answers`, `day_ratings` and the habits tables.

- **What it asks** (8:30 PM, silent if everything is already filled in): the day rating, every active yes/no journal
  question (Whoop-style: alcohol, caffeine after 2 PM, THC, screens in bed, late meal, sick or sore, stressful day,
  sex, date night; add or pause more), and any daily habit not logged yet. One reply records it all, e.g.
  "7, busy but good. alcohol yes, rest no, did stretching" (`journal_store.py checkin record`).
- **Explicit answers**: yes, no, or unanswered. Unanswered days stay out of the analysis, unlike count-only habits,
  where an unlogged day reads as "no". The former count-only habits Alcohol, THC, Sex and Date night were moved into the
  journal (logged days became "yes", earlier days unanswered) and paused.
- **Timing**: the card shows today's check-in from 5 PM; before that, yesterday's if it's unfinished.
- **Impact** (Wellness → Drivers): the average day rating (same day) and next-night HRV, sleep score and resting HR on
  "yes" vs "no" days, shown once a question has 5 of each. Answers also flow into the insights analysis as
  `journal:<slug>` (the `journal` insights source).
- To add another nightly input, add it to `pending`, `checkin_message` and `record` in `scripts/journal.py`.

Register once (replaces the old `dayrating-nightly-nudge`, now disabled):
```
python scripts/scheduler_store.py add --name "nightly-checkin" \
  --prompt "Delegate to the journal subagent to run the nightly check-in (send the check-in message exactly as the script gives it; silent if everything for today is already filled in)." \
  --cron "30 20 * * *" --target-chat-id "<your chat_id>"
python scripts/scheduler_store.py disable --name "dayrating-nightly-nudge"
```

## Day rating and insights

A daily happiness score plus the "why". The `dayrating` subagent and the dashboard's **Check-in** page (`/checkin`, Me tab) and Insights page run the
**same code** (`scripts/dayrating.py`, `scripts/insights.py`) against the same tables (`day_ratings`, `insight_daily`,
`insight_reports`).

- **Rating**: one 1-10 score and a short note per day, from Telegram ("today was a 7, long day") or the dashboard;
  rating a day again edits it, past days can be backfilled. The nightly check-in (8:30 PM, `journal` agent, see above) asks for it.
- **Sources** (`scripts/insight_sources/`, one module each, auto-discovered): every habit done / not done, day of
  week, daily weather (free Open-Meteo archive, location from `weather_config`), and COROS recovery data (overnight
  HRV, resting heart rate, fatigue, training load, from the same login as the workout sync), and calendar load
  (events, hours booked, Caroline's shift, travel, kid activities). Google Calendar is only reachable through the
  agent's connector, so the `dayrating` agent reads events and runs `dayrating_store.py calendar sync`; the rules live
  in `scripts/calendar_load.py`, which excludes the Meal Planning, Running and holiday calendars. Values are stored per day
  in `insight_daily`, so adding a source needs no other wiring. Monarch spending is planned but charges post late, so
  it must skip or discount the most recent day.
- **Analysis**: for each metric (same day and previous day) it reports the rating with vs without it or a rank
  correlation, a permutation p-value and a confidence label (solid / emerging / hint). Under 10 rated days it says
  there isn't enough data. Many metrics are tested at once, so findings are leads, not proof.
- **AI story**: the `dayrating` subagent (Sonnet) turns the statistics plus the user's notes into a narrative, saves it
  (`insights save`, shown on `/rating/insights`) and suggests new data sources or manual questions to add.

Register once:
```
python scripts/scheduler_store.py add --name "dayrating-weekly-insights" \
  --prompt "Delegate to the dayrating subagent to run the weekly insights report (refresh sources, analyze, write and save the story, reply with a short summary and up to two suggestions for new data sources)." \
  --cron "30 19 * * 0" --target-chat-id "<your chat_id>"
```

Connector systems (Todoist, Calendar, Gmail, Home Assistant) are pinged by the `myhub` agent twice a day:
```
python scripts/scheduler_store.py add --name "system-ping-morning" \
  --prompt "Delegate to the myhub subagent to ping the connector systems (Todoist, Google Calendar, Gmail, Home Assistant) and record each result. Silent unless a ping failed." \
  --cron "35 6 * * *" --target-chat-id "<your chat_id>"   # and "5 17 * * *" for system-ping-evening
```

## My Hub (the personal command page)

Since the 2026-10-19 dashboard redesign the hub is the dashboard's **Today** page (`/`; `/me` redirects there, the
old page is at `/archive/me`), merged with the old Summary page's ask box and glance tiles. It shows everything that matters to the person: a ranked **Needs attention**
strip (urgent / to act on / to note, never hidden, "All clear" when empty) on top, then today's calendar, dinner and
tasks, mood, COROS recovery/sleep/vitals/wellness check, habits, weather, household (chores, maintenance), the latest
insight and system health (expired logins, failed scheduled tasks). The `myhub` subagent and the page run the **same code**
(`scripts/myhub.py` and the providers in `scripts/hub_providers/`); `/api/me` is the JSON twin (`/api/today` adds the
Summary tiles plus agent and login-expiry items).

- **Self-completing**: each area is a provider module; its needs-attention items flow into the strip with no other wiring,
  and a test fails if a required area has no provider. A provider that errors shows up as a visible warning, never silently
  missing.
- **Calendar and tasks** come from a snapshot (`hub_items` table) because Google Calendar and Todoist are only reachable
  through the agent connectors: the `myhub` agent copies them in via `myhub_store.py snapshot set` on a schedule. The page
  says when they were last synced and flags them when older than 36 hours.
- **No repeat nagging**: after each refresh `myhub_store.py notify-check` decides what to message. An item is announced once
  when new (and again only if it escalates); urgent items repeat every 6 h while awake; warn items are only repeated by one
  morning digest (the first refresh at 7am); info items never message; from 8pm to 7am only a new urgent item gets through.
  If the calendar or task snapshot is more than 4 h old (a failed refresh, e.g. Todoist down), every message gets a "could
  not refresh" caveat and, when nothing else is due, a warning goes out alone (every 6 h while awake). The agent retries a
  failed `find-tasks`/`list_events` once or twice first. What was told is remembered in `hub_notified`. **Dismissals are one shared list** (`hub_dismissed`) used by the Today
  page, the archived Summary and My Hub pages, the agent and the notifier, so dismissing an item anywhere hides it everywhere and stops it
  notifying; the old `state/dashboard_dismissed.json` is imported once and renamed.
- Not on the hub yet: Hy-Vee cart state; see `docs/TODO.md`. (Money now has its own Today tile from the Monarch snapshot.)

Register once (calendar and tasks need the agent, so every 2 hours 7am-9pm):
```
python scripts/scheduler_store.py add --name "myhub-refresh" \
  --prompt "Delegate to the myhub subagent to refresh the hub's calendar and task snapshot then run notify-check and send only what it says (otherwise stay silent)." \
  --cron "0 7-21/2 * * *" --target-chat-id "<your chat_id>"
```
Sleep, recovery, weather and habits are script-reachable, so `assistant-insights-refresh.timer` refreshes them every
30 minutes (6am-10pm, no model tokens; `insights refresh --days 3 --source coros_health,weather,habits,journal`). The hub footer
shows each source's last refresh (amber when older than twice its interval) and a **Refresh now** button for the script tier.
Install: `scripts/install_systemd.sh assistant-insights-refresh.timer`.

## Chores agent (kid chore chart)

A chore chart with dollar payouts and streaks for the kids. The `chores` subagent, the adult
**Chores** dashboard section and the kids' **/kids** page all run the **same code**
(`scripts/chores.py`) against the same tables (`chore_*`), so "Claire fed the dog" in Telegram
ticks the box on the kids' screen and a tap on the screen is visible to the agent. Streak math is
reused from the habit tracker (`habits.habit_status`).

- **Chores** are daily or weekly (optionally due on set weekdays, e.g. trash Tue/Fri), each worth
  a fixed dollar amount per completion, assigned to one or more kids; each kid earns and streaks separately.
- **Kids' screen (`/kids`)**: one shared, big-button page (Tailscale-only, no login). Tap a chore =
  done; the balance ticks up with confetti and a chime (with a mute button), plus streak flames,
  a week strip and badges. (Reading chores aloud was removed to keep it simple; easy to add back.)
- **Trust mode**: a tap counts immediately; guardrails instead of approval (no double-dipping per
  day/week, kids can only tick or undo *today*, every payout is in the ledger).
- **Money**: integer cents. Balance = earned - payouts, never negative. Parents record payouts after
  handing over cash (dashboard Money page, or "I paid Ruth $2" to the agent). Deliberately simple for
  now: no bonuses, reward shop, savings goals or manual adjustments (ideas shelf in `docs/TODO.md`).
- **Weekly payday summary** (scheduled, Sunday evening): who is owed what, earned this week, chores
  done, best streaks. Optional **afternoon check** tells the adults which chores are still open
  (silent when everyone is done).

One-time setup: add the kids and chores (dashboard **Chores → Chores & kids**, or ask the agent), then
register the scheduled tasks -
```
python scripts/scheduler_store.py add --name "chores-weekly-payday" \
  --prompt "Delegate to the chores subagent to run the weekly payday summary." \
  --cron "0 17 * * 0" --target-chat-id "<your chat_id>"
python scripts/scheduler_store.py add --name "chores-afternoon-check" \
  --prompt "Delegate to the chores subagent to run the afternoon open-chores check." \
  --cron "30 16 * * 1-5" --target-chat-id "<your chat_id>"
```
Put a tablet or old phone on the tailnet, open `http://<tailscale-ip>:5151/kids` and add it to the home
screen.

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
  namespaced `intent__Hass*` etc. tools + `homeassistant__GetLiveContext`) for ordinary, low-friction control/query — no setup
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
# 2. Add to ~/.config/scherbring-assistant/.env (mode 600, never commit):
#      HOMEASSISTANT_URL=http://homeassistant.local:8123
#      HOMEASSISTANT_TOKEN=<paste token>
# 3. scripts/setup_wsl.sh installs ha-mcp (pinned) into vendor/ha-mcp/.venv. It needs Python >= 3.13,
#    so that venv is created with uv, separate from the main .venv.
# 4. Start it, and have it start at boot:
scripts/install_systemd.sh assistant-ha-mcp.service
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

One-time setup: add `COROS_USERNAME`/`COROS_PASSWORD` to `~/.config/scherbring-assistant/.env`
(`scripts/setup_wsl.sh` installs Playwright and Chromium). Always pass `--headless` to the COROS
scripts when running them by hand on a machine with no display. Weekly trigger:
`weekly-fitness-plan` scheduled task, Sundays 6pm — see **Scheduled tasks** above.

**Workouts habit auto-log:** `scripts/fitness/workout_sync.py` reads recorded COROS activities (any type; several in a day = one workout; the plan/calendar is ignored) and logs one completion per day for the `Workouts` habit, idempotently. Registered as `workouts-habit-sync` (daily 6am, silent unless COROS login fails): `python scripts/scheduler_store.py add --name "workouts-habit-sync" --prompt "Run `python scripts/fitness/workout_sync.py --days 3` (exactly that command, via Bash, and nothing else: do not run any follow-up command to check the exit code). It logs the Workouts habit for any recent day with a COROS activity. If the command prints a normal result, reply with nothing (stay silent). If it prints an error or a traceback, reply with one short line saying the COROS workout sync failed and the error message." --cron "0 6 * * *" --target-chat-id "<chat_id>"`. Reads the COROS health MCP (history from ~9/5), falling back to the web API; `--source web` forces web for deeper backfills. Backfill: `python scripts/fitness/workout_sync.py --days 35`.

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

These agents talk to Monarch through a **local, vendored MCP server**. Monarch's **official**
connector (`mcp__claude_ai_Monarch_Money__*`) is authorized too, but only as the `finance` agent's
fallback when the local login expires: it can't see Finicity-connected accounts (Fidelity, Alliance
Bernstein), and the Core plan caps weekly usage (see `docs/OPERATIONS.md`). The local server is `vendor/monarch-mcp-server/` (the community
[`robcerda/monarch-mcp-server`](https://github.com/robcerda/monarch-mcp-server), built on
the actively-maintained `MonarchMoneyCommunity` fork), registered as `monarch` in
`.mcp.json`. `scripts/setup_wsl.sh` clones and installs it at a pinned commit. Credentials never
pass through Claude. Auth is a one-time terminal step that saves a session file at
`~/.monarch-mcp-server/token` (mode 600; there is no OS keyring on this setup):

```
cd vendor/monarch-mcp-server
.venv/bin/python login_setup.py   # choose option 1: paste the cookie header from app.monarch.com
```

The `check_auth_status` tool ignores that file and reports "not authenticated" even when the login
works. Judge auth by a real read such as `get_accounts`. The session lasts roughly two weeks;
`scripts/credential_check.py` tests it for real and tells you if it is down; the **Monarch Login** / **Google Drive Login** desktop shortcuts (`scripts/connection_refresh.py`) refresh it.

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

- Runs a **Monte Carlo retirement projection** (`scripts/retirement_model.py`, numpy) through
  the shared service `scripts/retirement.py` / CLI `scripts/retirement_store.py`. Inputs are
  derived in code from the local Monarch snapshot: retirement balance (pretax + Roth + HSA +
  taxable; kids' 529/UTMA money and cash excluded), allocation from holdings (synced daily into
  `money_holdings`), and trailing-12-month savings. Returns come from forward-looking
  capital-market assumptions (`scripts/retirement_data/cma.json`) with a glide path. User
  choices live in `finance_config` (`retirement_assumptions`, plus `retirement_accounts` and
  `retirement_securities` overrides) → success probability ± simulation noise, p10/p50/p90
  bands in nominal and today's dollars, and what-if scenarios.
- Household model: taxable / pre-tax / Roth buckets plus the kids' 529s, after-tax spending
  with withdrawals grossed up for federal + Minnesota tax, RMDs and early-withdrawal
  penalties, Social Security per person with claiming-age rules and a survivor case, and
  dated cash flows (mortgage through payoff, healthcare before and after Medicare, college).
  Household facts live in `retirement_assumptions.household`.
- Produces a **deep, user-editable `.xlsx` workbook** (`scripts/retirement_workbook.py`) —
  live Excel formulas on an editable Assumptions tab, a Monte Carlo band tab, an Accounts tab,
  one tab per scenario — uploaded to a `Retirement/` Drive subfolder as a new date-stamped file every run via `scripts/drive_upload.py`
  (the folder of dated files is the archive; nothing is overwritten).
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

### Money home page and the Monarch snapshot

The dashboard's **Money** tab opens on an overview: this month's fixed / flexible / non-monthly / total
budget against plan (red, yellow or green; flexible is judged against the month's pace), projected
month-end spending, spending so far vs last month and the 3-month normal, cash-flow savings rate,
emergency-fund runway, card balances, net worth (30-day and year-to-date change),
over-budget categories, the retirement model's last verdict, and the "keeping up" counts:
transactions waiting for review in Monarch (posted only) and transactions since 2025-06-01 without a WHO
tag. Tagging and Reports & planning are its sub-pages.

The numbers come from a local copy of Monarch (`money_*` tables), refreshed by
`scripts/money_sync.py` every 30 minutes from 6am to 11pm (`assistant-money-sync.timer`; a full
pull since 2025-06-01 once a day, the last 120 days otherwise) and by the page's **Refresh now**
button. Each sync also settles the finance agent's tagging log against what you did in Monarch
directly, so "awaiting decision" stops counting transactions you already tagged there. The service
is `scripts/money.py`; the `finance` agent reads the same numbers with `scripts/money_store.py`, and
thresholds are adjustable with `money_store.py config set`. Install the timer:
`scripts/install_systemd.sh assistant-money-sync.timer`.

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

One-time setup — put `HYVEE_USERNAME`/`HYVEE_PASSWORD` in `~/.config/scherbring-assistant/.env`
(`scripts/setup_wsl.sh` already installed Playwright and its Chromium), then:
```
python scripts/hyvee/cart_ops.py sync-history
python scripts/hyvee_store.py history ingest --json <json_path from sync-history>
python scripts/hyvee_store.py seed --items "milk,eggs,bread,bananas,..."
```
Hy-Vee shows a CAPTCHA on login, so the headless login cannot refresh an expired session by
itself. There is one way to sign in: the **Hy-Vee Login** shortcut on the Windows desktop (a copy is in
`scripts/hyvee/`), which runs `login_test.py --login-only --manual-wait 300`. There is one check,
`scripts/hyvee/hyvee_connection.py`, run every 2 hours; it alerts once (with the reason) when the login is really
expired and once when it recovers. Build-only, always: `cart_ops.py` has no checkout command by design.

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
2. The agent delegates to the `todoist` subagent.
3. Subagent creates the task in Todoist (creating the list if needed) and writes a row
   to the state store.
4. The agent replies in the same Telegram chat.
5. Send a follow-up: **"what did I just add, and to which list?"**
6. The agent re-delegates to the `todoist` subagent.
7. Subagent reads its own saved record and answers **with continuity** — not a generic
   restatement, not "I don't have that context."

Also confirm:
- `git status` shows no secrets tracked (drop a dummy secret in `.env` / `state/*.db` —
  it must not appear).
- Messaging from a non-allowlisted Telegram account does **not** reach the agent.

## Out of scope for Phase 1

Email delivery, subagents beyond Todoist, containerization, Remote Control,
notification-formatting skills. (Cron-style scheduling and a custom local channel were
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
