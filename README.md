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
| `.claude/agents/meal-planner.md` | Meal-planner subagent (recipe library, Google Calendar, Todoist Shopping List) |
| `.claude/agents/scheduler.md` | Scheduler subagent — creates/lists/pauses/deletes proactive scheduled tasks |
| `.mcp.json` | Project MCP config (Todoist HTTP/OAuth, local `scheduler` channel). Gitignored. See `.mcp.json.example`. |
| `state/schema.sql` | SQLite schema for `agent_results`, `recipes`, `scheduled_tasks`, `scheduled_task_runs` |
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

- [ ] **Lawn & garden agent** — regular checks for spot-spraying weeds, a fertilizer
      schedule, spring/fall + regular pruning/trimming, combined with weather and the
      family calendar to actually get tasks scheduled.
- [ ] **Personal finance agent** — needs a Monarch Money MCP server (all financial data is
      aggregated there). The official server is currently paused; look into unofficial/
      community alternatives.
- [ ] **Shopping cart builder** — build (not place) orders at Hy-Vee and/or Target, comparing
      price across stores, and remembering which specific variant of a regular item ("milk")
      to add. Stops short of checkout — a human reviews and places the order. Note: Hy-Vee
      auth has been difficult in the past.
- [ ] **Weather reminders** — proactive nudges ahead of incoming weather (shovel snow, bring
      in cushions, etc.).
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
- [ ] **Birthday/gift-reminder agent** — the Todoist subagent already keeps per-person Gift
      Ideas sub-lists; a proactive nudge ahead of a birthday/anniversary using what's already
      on that person's list.
- [ ] **Home maintenance agent** — same shape as the lawn & garden agent, but indoor: HVAC
      filters, smoke detector batteries, gutter cleaning, on a recurring cadence + calendar.

### Someday / needs more refinement

Lower priority, idea-only — not enough shape yet to even brainstorm.

- Trip/travel planning (packing lists, itineraries tied to the calendar)
- Family "newsletter" recap — a personal weekly digest, possibly reusing the existing
  `/newsletter` skill's pattern (currently built for technical/business newsletters)
