# Project instructions

## Scheduler self-arming (do this first, every turn)

<EXTREMELY_IMPORTANT>
Before doing anything else this turn — before replying, before delegating to a subagent,
before any other tool call — call `CronList`. This is not optional and not conditional on
what the request is about. Do it even for "you there?", even for a one-word reply, even if
it feels irrelevant to what the user asked. Skipping this is the single most common way this
feature silently breaks, because nothing else will notice or remind you.

**Why:** the scheduled-tasks feature (recurring/one-time prompts managed by the `scheduler`
subagent, stored in `state/agent_results.db` via `scripts/scheduler_store.py`) has no external
poller — it depends entirely on the orchestrator arming an in-session poll loop via the
`CronCreate` tool. `CronCreate` jobs are session-only (in-memory, gone the moment this `claude`
process exits), so if this loop isn't re-armed on every fresh session/resume, scheduled tasks
just silently stop firing. There is no other mechanism keeping this alive.

**What to do**, immediately, before anything else:
1. Call `CronList`.
2. If it does not contain a job for the scheduler poll loop (check by cron expression
   `*/2 * * * *`, or by comparing against `state/scheduler_loop_state.json`'s `job_id` if that
   file exists), arm one right now:
   ```
   CronCreate(recurring: true, cron: "*/2 * * * *", prompt: <the recurring-job prompt below>)
   ```
   Then write `state/scheduler_loop_state.json` as
   `{"armed_at": "<iso>", "job_id": "<id>", "last_tick": "<iso>"}` (create the file if absent).
3. If it's already armed, do nothing further for this step — proceed to the user's actual
   request as normal.

A separate OS-level watchdog (`scripts/watchdog_scheduler_health.ps1`) independently checks
`state/scheduler_loop_state.json`'s `last_tick` and force-restarts the orchestrator if it goes
stale — but that only catches a loop that was armed and then died, not one that was never armed
in the first place. Step 1 above is the only thing that catches that.
</EXTREMELY_IMPORTANT>

**The recurring job's prompt**, run every time it fires (every ~2 minutes):

1. Update `state/scheduler_loop_state.json`'s `last_tick` to the current time (heartbeat, read by
   the external watchdog above).
2. If `armed_at` in that same file is more than ~6 days old, call `CronCreate` again the same way
   to arm a fresh replacement job, and update `armed_at`/`job_id` to the new one. (Don't delete the
   old job — let it expire naturally at its own 7-day mark; a few hours of overlap where two jobs
   both check for due work is harmless.) This keeps the loop alive indefinitely without ever
   hitting `CronCreate`'s 7-day recurring-job expiry, as long as the loop fires at all within any
   7-day span — which it always does, since it fires every 2 minutes.
3. Run `python scripts/scheduler_store.py due` and parse the JSON result.
4. For each due task: `python scripts/scheduler_store.py mark-dispatched --id <id> --run-at
   <due_run_at>`, then carry out the task's stored `prompt` field verbatim as an instruction
   (delegating to other subagents as needed), then reply into Telegram via
   `mcp__plugin_telegram_telegram__reply` using the task's `target_chat_id`, prefixed
   `📅 Scheduled: <name>`, then `python scripts/scheduler_store.py log-run --task-id <id> --run-at
   <due_run_at> --status ok|failed [--summary "..."]`.
5. If nothing is due, do nothing further — no reply, no noise.

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

## Telegram channel behavior

**Acknowledge long-running requests.** When a request arrives via the Telegram channel
(a `channel` event carrying a `chat_id`, not a terminal request) and it will take more than
a few seconds — especially before delegating to a long-running subagent such as `hyvee`
(Hy-Vee cart building) or `meal-planner`, or before any multi-step web automation or bulk
job — FIRST send a brief one-line acknowledgement to the user via the Telegram reply tool
(`mcp__plugin_telegram_telegram__reply`) using the incoming `chat_id`, then do the work,
then send the result.

- Send a SINGLE acknowledgement at the start that names the task, e.g. "🛒 On it — building
  your Hy-Vee cart. This usually takes a couple of minutes; I'll send the summary when it's
  ready."
- Do NOT send interim progress spam — one start ack plus the final result is enough.
- Applies ONLY to Telegram-originated requests; never send Telegram messages for terminal
  requests.
- Skip the ack for quick requests that answer in a few seconds — it's only for genuinely
  long-running work.
