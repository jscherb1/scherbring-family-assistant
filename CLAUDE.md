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
