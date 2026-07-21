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
| `.mcp.json` | Project MCP config (Todoist HTTP/OAuth, local `scheduler` channel). Gitignored. See `.mcp.json.example`. |
| `state/schema.sql` | SQLite schema for `agent_results`, `recipes`, `scheduled_tasks`, `scheduled_task_runs`, `kid_memories`, `kid_memory_triggers`, `lawn_garden_plants`, `lawn_garden_products`, `lawn_garden_program`, `lawn_garden_treatments`, `lawn_garden_issues`, `lawn_garden_config`, `profile_people`, `profile_people_facts`, `profile_facts`, `weather_config`, `weather_alerts`, `home_maintenance_items`, `home_maintenance_completions` |
| `state/agent_results.db` | The state store (auto-created; gitignored — holds personal data) |
| `scripts/state_store.py` | Zero-dep CLI the subagents call to write/read continuity results |
| `scripts/recipes_store.py` | Zero-dep CLI for the recipe library (list/add/feedback/mark-cooked) |
| `scripts/scheduler_store.py` | Zero-dep CLI for the scheduled-tasks registry (add/list/enable/disable/delete/due/log-run) |
| `scripts/scheduler_channel/` | Local one-way MCP channel (Bun) that delivers due tasks into the orchestrator's session |
| `scripts/scheduler_poll.py` | Poller driver — finds due tasks and POSTs them to the scheduler channel |
| `scripts/run_scheduler_poll.ps1` | Wrapper the poller Scheduled Task invokes; logs to `state/logs/` |
| `scripts/register_scheduler_poller_task.ps1` | One-time setup for the poller's Scheduled Task (fires every ~2 min) |
| `scripts/start_orchestrator.ps1` | Idempotent launcher — skips if already running, restarts on exit |
| `scripts/orchestrator_status.ps1` | Read-only check for whether the orchestrator is running |
| `scripts/register_orchestrator_task.ps1` | One-time setup for the auto-start-at-logon Scheduled Task |
| `scripts/kid_memories_store.py` | Zero-dep CLI for kid memories (add/update/get/list/mark-drive-synced/mark-drive-failed/add-trigger/triggers) |
| `scripts/lawn_garden_store.py` | Zero-dep CLI for lawn & garden data (plant/product/program/treatment/issue/config sub-commands) |
| `scripts/profile_store.py` | Zero-dep CLI for the personal profile store (person/fact/global-fact sub-commands) — any subagent can call it directly |
| `scripts/weather_store.py` | Zero-dep CLI for weather-reminders data (config/alert sub-commands) |
| `scripts/home_maintenance_store.py` | Zero-dep CLI for home maintenance data (item/completion/due sub-commands) |
| `.env.example` | Env template (no real secrets needed for Phase 1) |

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

## Scheduled tasks (proactive, recurring prompts)

Lets you ask the orchestrator to do something on a schedule — "every Sunday at 9am,
plan next week's dinners and post it" — and have the result posted into this same
Telegram chat, clearly marked `📅 Scheduled: <name>` so it's never mistaken for a
reply to something you said. Architecture: a shared Windows Scheduled Task polls a
SQLite registry every ~2 minutes and, for anything due, pushes it into the
already-running orchestrator session via a small local one-way MCP channel
(`scripts/scheduler_channel/`) — the orchestrator then does the work and replies using
the Telegram connection it already holds. Adding a **new** scheduled task afterward is
just a conversation with the `scheduler` subagent (or a row in the registry) — it never
touches Windows Task Scheduler again.

One-time setup:

1. **Install the channel server's dependency** (once):
   ```
   cd scripts\scheduler_channel
   bun install
   ```
2. **Register the poller's Scheduled Task**:
   ```
   powershell -ExecutionPolicy Bypass -File scripts\register_scheduler_poller_task.ps1
   ```
3. **Known limitation — confirmed, not just a risk:** Claude Code's dev-channel warning
   dialog (required because a custom channel isn't on the research-preview allowlist)
   **reappears on every single launch**, not just the first. This means
   `start_orchestrator.ps1`'s unattended restart (crash, logon) currently **cannot**
   auto-recover — every start/restart needs someone to manually click through the
   dialog. Until this is addressed, treat the orchestrator as **manual-start only**:
   ```
   powershell -ExecutionPolicy Bypass -File scripts\start_orchestrator.ps1
   ```
   **TODO (follow-up, not yet done):** if the manual-start requirement becomes
   annoying in practice, switch to the fallback already scoped in the design spec:
   drop the custom local channel entirely and have the orchestrator self-arm a
   session-scoped `CronCreate` poll loop on startup (via a `SessionStart` hook) that
   polls `scripts/scheduler_store.py due` directly and acts on it in-session, instead
   of a separate Windows-Task-driven poller pushing into a channel. Trade-off: the
   poll loop only runs while the orchestrator session is alive (already true today
   either way) and needs to re-arm itself within Claude Code's 7-day recurring-task
   expiry.
4. **Seed the daily heartbeat task** (optional but recommended) — reviews recent run
   history and posts a digest only if something is overdue or failed. Needs your
   Telegram `chat_id` (visible in the orchestrator's transcript from any message
   you've sent it, or ask it "what's my chat_id"):
   ```
   python scripts/scheduler_store.py add --name "daily-heartbeat" \
     --prompt "Review scripts/scheduler_store.py runs from the last 24 hours for any status of dispatch_failed or failed, or any enabled task with no run in the last 24 hours that should have fired. If everything is healthy, reply nothing. Otherwise post a short digest of what needs attention." \
     --cron "0 8 * * *" --target-chat-id "<your chat_id>"
   ```
5. Try it: message the bot "every day at [a couple minutes from now], say hello" and
   confirm the scheduled reply arrives prefixed `📅 Scheduled:`.

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

- [ ] **Personal finance agent** — needs a Monarch Money MCP server (all financial data is
      aggregated there). The official server is currently paused; look into unofficial/
      community alternatives.
- [ ] **Shopping cart builder** — build (not place) orders at Hy-Vee and/or Target, comparing
      price across stores, and remembering which specific variant of a regular item ("milk")
      to add. Stops short of checkout — a human reviews and places the order. Note: Hy-Vee
      auth has been difficult in the past.
- [x] **Weather reminders** — proactive nudges ahead of incoming weather (shovel snow, bring
      in cushions, etc.). Built: `weather-reminders` subagent, see **Weather reminders agent**
      above.
- [ ] **Shopping assistant (deal-watching)** — watches for deals on non-urgent wanted items
      across stores, Craigslist, Facebook Marketplace, etc.
- [ ] **Google Chat bridge** — a second chat channel (alongside Telegram) so the assistant
      is reachable from Google Chat too.
- [ ] **Grocery list from pantry/fridge photos** — snap a picture of what's on hand and
      generate the grocery list from what's actually missing.
- [ ] **Smart home agent** — the Home Assistant MCP server is already connected but unused
      (lights, climate, media). Combine with the calendar for routines like "goodnight" or
      "leaving for school."
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
