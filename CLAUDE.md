# Project instructions

## Model policy

All subagents (`.claude/agents/*.md`) and any other configured AI usage in this repo
(cron/scheduled agents, channel configs, etc.) must default to **Sonnet** to conserve
cost/tokens.

- Every subagent's frontmatter must set `model: sonnet` explicitly.
- Do not use Opus or any higher-tier/more expensive model without asking the user first
  and getting explicit confirmation that the cost tradeoff is worth it for that specific
  use case.
- If you encounter or are about to add a config that specifies Opus (or anything above
  Sonnet), stop and flag it to the user before proceeding.

## New feature standard: agent + UI on one shared path

Every new feature or enhancement must be usable **both** through an AI agent **and** through the
dashboard UI, and both must run the **same code against the same data**, so nothing is built or
maintained twice. Reference implementation: habits (`scripts/habits.py`).

1. **One shared service module** (`scripts/<feature>.py`, stdlib only) holds all business rules and
   reads/writes (validation, calculations, logging). It takes an optional `db_path` and raises a typed
   error carrying a CLI exit code. No logic lives anywhere else.
2. **Agent surface** = a thin CLI `scripts/<feature>_store.py` over that service (JSON out, errors on stderr,
   exit 1 not found / 2 bad input) plus a dedicated subagent `.claude/agents/<feature>.md` (`model: sonnet`)
   that knows every task: create/edit, record, recall/status, and any scheduled/proactive check.
3. **UI surface** = a dashboard section (`dashboard/data/<feature>.py` reader + `dashboard/routers/<feature>.py`
   + templates + a `SECTIONS` entry in `dashboard/web.py`, and a Summary tile in `dashboard/data/summary.py`
   when relevant). The data module and the write routes call the **service directly**; they must not
   re-implement rules or run their own SQL for them. `/api/*` JSON twins return the same service output.
   UI writes use the same-origin guard from `dashboard/routers/ask.py`; add new write routes to the allow-list
   in `tests/test_dashboard.py::test_read_only_except_allowlisted_write_routes`.
4. **Same data model**: tables appended to `state/schema.sql` (applied on connect), uuid ids, local-ISO timestamps.
5. **Tests**: unit tests for the service rules and CLI (`tests/test_<feature>.py`), dashboard route tests, and one
   test proving UI and service see the same data.
6. **Wire-up checklist**: allow rule `Bash(python scripts/<feature>_store.py *)` in `.claude/settings.json`; rows in
   `README.md` (agents table, scripts table, schema table list, feature section with any scheduler registration
   command), `GETTING_STARTED.md` capability table, `dashboard/README.md` sections table; `docs/TODO.md` for anything
   untested or manual; scheduled tasks documented with their `scheduler_store.py add` command.

Older features (home-maintenance, lawn-garden, etc.) still duplicate logic between their CLI and the dashboard
reader; migrate them to this pattern opportunistically (tracked in `docs/TODO.md`).

## Development process

Applies to all development work (features, fixes, docs), whether it comes from a terminal session or Telegram.

1. **Present a plan before building.** Confirm what will be done and how, with suggestions and any
   clarifying questions, and wait for approval before changing code. Depth scales with the channel:
   in a Claude Code session give a fuller plan (files touched, approach, tradeoffs, test plan); over
   Telegram keep it a short phone-readable summary (what, how, open questions). Skip the plan only for
   trivial edits the user has already described exactly.
2. **Always work on a separate branch and merge through a PR.** Never commit directly to `main`. Branch,
   commit, open a PR, and merge only when the user says to.
3. **Check whether a service restart is needed.** Before calling work done, work out which running
   service picks up the change (dashboard, agent worker, orchestrator, scheduler; see
   `docs/OPERATIONS.md`) and restart it if needed, or tell the user it needs one. New or edited
   `.claude/agents/*.md` files also need `/reload-plugins`. Say in the final message what was
   restarted, and whether the change was verified live or only by tests.

## Open to-dos and runbook

Outstanding work (untested scheduled tasks, manual user actions, deferred decisions) is tracked
in `docs/TODO.md`; read it before starting related work and update it when you finish or add
something. Day-to-day operation (status, logs, restarts, backups, expiring logins) is in
`docs/OPERATIONS.md`.

## Telegram via the bridge (current setup)

Telegram messages now reach you through `scripts/telegram_bridge.py` + `scripts/agent_worker.py`
(see `docs/OPERATIONS.md`, "Telegram bridge"), not the Telegram plugin. You are in this mode if
there is no `mcp__plugin_telegram_telegram__reply` tool and messages arrive wrapped in a
`<channel source=... chat_id=...>` tag. In this mode:

- **Your final message IS the reply.** It is delivered to the user on the channel the message came
  from. Do not call a Telegram tool or run `telegram_send.py` for normal replies.
- **The bridge handles acknowledgements** (👀 reaction, typing, and a "working on it" notice after
  ~20 s). Don't send your own; the plugin-era "always ack >3 s" rule below is satisfied for you.
- **Never use `AskUserQuestion`.** If you need an answer, ask in your final message; the user's
  answer arrives as the next message.
- A `[Earlier notes]` block holds things already delivered to the user outside this conversation
  (e.g. scheduled-task results). It is context, not a request; use it when they ask follow-ups.
- A `[Retry #N ...]` marker means an earlier attempt didn't finish and may have partly run: check
  current state before repeating any side effect (adding tasks, carts, tags, calendar events).
- Run things in the foreground. Background tasks are disabled; their completion can't reach anyone.
- Operating it: `scripts/agent_ctl.py status|tail|say|pause|mode`.

The sections below describe the **plugin-era** behavior. They apply only if the Telegram plugin
tool is present (e.g. after `agent_ctl.py mode plugin`).

## Telegram channel behavior (plugin mode only)

**Acknowledge any request expected to take more than ~3 seconds. This is a hard rule,
always followed, no exceptions.** When a request arrives via the Telegram channel
(a `channel` event carrying a `chat_id`, not a terminal request) and the work — ANY
work, not just subagent delegation — will take more than a few seconds (delegating to a
subagent like `hyvee`, `meal-planner`, `kids-memory`, etc.; multi-step web automation;
bulk jobs; multi-file investigation; running several tool calls in sequence), FIRST send
a brief one-line acknowledgement to the user via the Telegram reply tool
(`mcp__plugin_telegram_telegram__reply`) using the incoming `chat_id`, then do the work,
then send the result.

- Send a SINGLE acknowledgement at the start that names the task, e.g. "🛒 On it — building
  your Hy-Vee cart. This usually takes a couple of minutes; I'll send the summary when it's
  ready." or "💙 On it — saving that memory for Ruth."
- Do NOT send interim progress spam — one start ack plus the final result is enough.
- Applies ONLY to Telegram-originated requests; never send Telegram messages for terminal
  requests.
- Skip the ack ONLY for requests you're confident will answer in a couple seconds (a quick
  lookup, a single fast tool call). If there's any doubt whether it'll take longer, send the
  ack — a redundant ack costs nothing, a missing one leaves the user wondering if the message
  landed.

**Always ask and reply through Telegram, never through `AskUserQuestion`.** For a
request that arrived via the Telegram channel, the user only sees messages sent with
`mcp__plugin_telegram_telegram__reply` (or the `telegram_send.py` fallback below) — they
are not attached to this terminal session, so `AskUserQuestion` never reaches them and
will silently hang or get skipped. This applies to every reply, not just clarifying
questions: acknowledgements, results, and any question you need answered mid-task must
all go out via the Telegram reply tool (or its fallback). Only use `AskUserQuestion` for
requests that originated in the terminal.

**If the Telegram reply tool errors or isn't available.** The `mcp__plugin_telegram_telegram__reply`
tool's binding can go stale mid-session (a known failure mode after an SSE stream reconnect —
the MCP server stays connected but the tool silently drops out of the render-time tool list).
If a call to it errors, or it's simply missing when you go to use it, do not give up or just
answer in plain text — fall back immediately to:

    python scripts/telegram_send.py "<message text>"

This sends directly via the Telegram Bot API, bypassing the broken tool binding, so the user
still gets the reply. It also stamps a state file that the `watchdog_telegram_health.py` check
picks up as an explicit unhealthy signal and uses to restart the orchestrator with a fresh,
working tool binding — so use this fallback every single time the real tool fails, not just
once. Restarting drops conversation context, so the watchdog sends its own heads-up before
doing so; you don't need to warn the user yourself, just keep using the fallback until it's
healed.
