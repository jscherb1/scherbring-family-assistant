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
