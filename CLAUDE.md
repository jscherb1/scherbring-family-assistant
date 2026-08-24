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
still gets the reply. It also stamps a state file that the `watchdog_telegram_health.ps1` task
picks up as an explicit unhealthy signal and uses to restart the orchestrator with a fresh,
working tool binding — so use this fallback every single time the real tool fails, not just
once. Restarting drops conversation context, so the watchdog sends its own heads-up before
doing so; you don't need to warn the user yourself, just keep using the fallback until it's
healed.
