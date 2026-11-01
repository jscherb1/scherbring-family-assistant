# Getting Started

This repo is a working personal AI assistant built on [Claude Code](https://claude.com/product/claude-code):
one agent you talk to over Telegram (or a terminal), which delegates to
scoped **subagents** (Todoist, meal planning, finance, kids' memories, lawn & garden,
smart home, fitness, etc.), each backed by a local SQLite store so it remembers things
between conversations.

It's yours to fork and make your own — the agents here reflect one family's life
(a Hy-Vee grocery cart builder, two kids' names hardcoded into `kids-memory`, a
Rochester, MN yard). Expect to prune what you don't need and rename/reshape the rest
for your own household. Think of this repo less as a product and more as a
**reference implementation and a starting skeleton**.

This doc is the fast path to a working assistant. `README.md` is the full reference —
every subagent, every script, every design decision — come back to it once things are
running.

## 1. Make it yours, not a fork you push back here

Don't push your changes to this repo. Instead:

```
# from a fresh clone/copy of this repo, pointed at your own remote
git remote remove origin
git remote add origin <your-new-private-repo-url>
git push -u origin main
```

Your `.env`, `.mcp.json`, `settings.local.json`, and the SQLite state store are all
gitignored on purpose — they hold real tokens and real personal data (your Todoist
tasks, your finances, your kids' names). Keep your remote **private**.

> **Don't run this from OneDrive or another corporate-synced folder.** Personal data
> and long-lived tokens will otherwise sync to a corporate cloud tenant. Pick a plain
> local folder and back it up via your own private git remote instead.

## 2. Prerequisites

- **Linux with systemd.** This was built and is run on **WSL2 Ubuntu 24.04**, and it works
  the same on plain Ubuntu or a small VPS. The always-on part is systemd user units. On WSL2 you also need `systemd=true` in `/etc/wsl.conf` and one Windows startup
  task that keeps the VM alive (see **Always-on** in `README.md`). macOS would need the
  units redone as launchd jobs.
- **[Claude Code](https://claude.com/product/claude-code)** installed and logged in
  (`claude` on your PATH).
- **Python 3.12+, sqlite3, git, [uv](https://docs.astral.sh/uv/) and
  [age](https://github.com/FiloSottile/age).** `scripts/setup_wsl.sh` builds the virtualenv
  and installs every Python dependency, Playwright's Chromium, and the pinned Home
  Assistant and Monarch servers, so you don't `pip install` anything by hand.

## 3. Core setup (do this first, everything else is optional)

This gets you the minimum working loop: Telegram → bridge → agent worker → Todoist subagent,
with memory. Full detail is in `README.md` under **Setup**, but the short version:

0. **Build the environment**: run `scripts/setup_wsl.sh` from the repo root. Then create
   your secrets file, which lives **outside the repo**:
   `~/.config/scherbring-assistant/.env` (directory mode 700, file mode 600), using
   `.env.example` as the list of keys. For the My Hub calendar and task refresh, also set
   `TODOIST_API_TOKEN` there (Todoist Settings > Integrations > Developer), plus the Home Assistant URL and token.
1. **Create a Telegram bot** via [@BotFather](https://t.me/BotFather); copy the token.
2. **Get your Telegram user ID** (e.g. via [@userinfobot](https://t.me/userinfobot)).
3. **Add the bot to your secrets file.** In `~/.config/scherbring-assistant/.env` set
   `TELEGRAM_BOT_TOKEN=<bot-token>` and `BRIDGE_ALLOWED_CHAT_IDS=<your chat id>`
   (comma-separated for several people). If you don't know your chat id yet, leave
   `BRIDGE_ALLOWED_CHAT_IDS` empty: the bridge then serves nobody and replies to any sender with
   their chat id, which you copy into `.env`. Messages from any other chat id are never served.
   Don't install the Telegram Claude plugin; it polls the same token and blocks the bridge.
4. **Start the bridge and the agent worker** (see the systemd step below; for a first look
   before installing, `scripts/agent_ctl.py status` shows whether both are up and
   `scripts/agent_ctl.py say "hello"` talks to the agent from the terminal).
5. **Pair and check**: message the bot. If you set the chat id, it answers; if it replied
   with a chat id instead, add that to `BRIDGE_ALLOWED_CHAT_IDS` and restart `assistant-bridge`.
6. **Todoist MCP** — `.mcp.json.example` shows the config; copy it to `.mcp.json`. The
   Todoist entry is the official hosted server (OAuth) — the first Todoist tool call
   pops a browser sign-in. Don't also run `claude mcp add todoist ...`, it'll cause an
   OAuth loop.
7. **Verify the loop**: text the bot "add buy milk to my shopping list," confirm it
   shows up in Todoist and the bot replies, then ask "what did I just add?" and confirm
   it remembers — that continuity (not just the round trip) is the real test.

Install the units so everything starts at boot and restarts if it crashes:
```
sudo loginctl enable-linger "$USER"
scripts/install_systemd.sh assistant-bridge.service assistant-agent-worker.service \
    assistant-scheduler.timer assistant-watchdog.timer assistant-credential-check.timer assistant-backup.timer
```
`docs/OPERATIONS.md` is the day-to-day runbook (status, logs, restart, backups, expiring
logins).

## 4. Everything else: ask your own assistant to build it

This is the part worth knowing about before you start reading agent-by-agent docs:
**most of the remaining setup is a conversation, not a checklist.** Each subagent
owns a small SQLite store seeded through its own CLI, and the fastest way to seed it
correctly is to just tell the running assistant what's true about your life and let
it call the right subagent. You don't need to hand-edit config files or read schemas.

Once your assistant is running, try prompts like:

- **"Remember that my wife's birthday is March 3rd, and I'm allergic to shellfish."**
  → routes to the `profile` subagent, which every other agent reads from for
  who's-who facts (dietary restrictions, sizes, birthdays).
- **"We have two kids, Sam (6) and Ellie (9). Set up the kids-memory agent for them."**
  → will need a real code change (the child names are currently hardcoded in
  `.claude/agents/kids-memory.md` and the schema comments), so expect it to propose an
  edit rather than just writing a database row — review and approve it.
- **"Set up the lawn & garden agent — I'm in <your city>, ~<X> sq ft yard, here's my
  equipment."** → seeds `lawn_garden_config` and offers to register the weekly
  Saturday check.
- **"Every Sunday at 9am, plan next week's dinners and post the grocery list."**
  → the `scheduler` subagent registers a recurring task — no systemd editing required
  for this one, it's a row in a SQLite registry the always-on dispatcher already watches.
- **"Track our HVAC filter, smoke detectors, and water softener salt."** → seeds
  `home-maintenance`'s recurring item list with sensible defaults you can adjust.
- **"What subagents do I have set up, and what's still missing?"** → a good sanity
  check any time; the assistant can read its own agent roster and state store.

For agents that need a **real credential** (Hy-Vee login, COROS login, Home Assistant
long-lived token, Monarch Money session, Google Drive OAuth), the assistant can't do
that part for you — those go in your secrets file (`~/.config/scherbring-assistant/.env`) or a one-time terminal
`login`/`auth` step, documented per-agent in `README.md`. Ask "what do I need to set up
the finance agent?" and it'll walk you through exactly that agent's section rather than
you hunting for it.

**When in doubt, just ask the assistant directly** — "how do I set up X" or "what
do you need from me to do Y" is a legitimate way to use this thing, not a fallback.
It has read access to its own docs and code.

## 5. What to prune or rename

Before going further, decide what you actually want from this list — everything is
independently deletable (delete the `.claude/agents/*.md` file, its `scripts/*_store.py`,
and its `state/schema.sql` tables):

| Keep if you want... | Agent(s) |
|---|---|
| Task/shopping lists | `todoist` |
| Meal planning + grocery list (recipes can also be added, edited and deleted in the dashboard) | `meal-planner` |
| Recurring proactive reminders | `scheduler` (needed by several others below) |
| A family memory log | `kids-memory` (rename/regeneralize — it's kid-specific today) |
| Lawn/yard care tracking | `lawn-garden` |
| Household facts (birthdays, allergies, sizes) | `profile` |
| Weather-driven prep nudges | `weather-reminders` |
| Golf rounds and handicap (incl. scorecard photos) | `golf` |
| Habit / streak tracking | `habits` |
| Daily 1-10 day rating with insights on what drives it | `dayrating` |
| Nightly check-in (rate the day, yes/no journal, open habits in one message) | `journal` |
| Wellness summary: recovery, sleep and HRV trends, what drives them (the Wellness dashboard pages) | `wellness` |
| "What needs my attention?", today/this week at a glance, the dashboard's Today page | `myhub` |
| Kid chore chart with dollar payouts and streaks | `chores` |
| Find, read or summarize email (read-only) | `gmail` |
| Home maintenance cadence tracking | `home-maintenance` |
| Smart home control (Home Assistant) | `home-assistant` |
| Home Assistant status, health and recommendations (also on the dashboard under Home > Home Assistant) | `ha-overview` |
| Weekly workout planning (COROS) | `fitness` |
| Monarch Money budgeting/tagging/reports/retirement modeling, plus the dashboard's Money overview (budget vs plan, spending, net worth, review queue) | `finance`, `finance-reporter`, `retirement`, `finance-advisor` |
| Hy-Vee grocery cart automation | `hyvee` |

None of the finance, fitness, Hy-Vee, or Home Assistant agents are useful without
accounts at those specific services — skip them entirely if they don't apply to you.

## 6. Guardrails worth knowing before you dig in

- **`CLAUDE.md`** enforces a model policy (subagents default to Sonnet, not Opus —
  flag it if you see otherwise) and Telegram-specific behavior (always acknowledge
  slow requests, always reply through Telegram tools rather than terminal-only
  prompts). Read it before changing agent frontmatter.
- Proactive/scheduled agents follow a **silent-when-healthy, propose-then-confirm**
  pattern — they never create a calendar event, Todoist task, or Monarch write
  unprompted. Keep that pattern if you extend them.
- Several agents (finance, retirement) are **read-only against Monarch by design** —
  only `finance` writes tags/rules, and only after you confirm.

## 7. Where to go next

- `README.md` — full reference: every subagent, every script, every table, full setup
  steps for each optional agent, and the backlog of what's built vs. still an idea.
- `docs/superpowers/specs/` — design docs for the larger features (finance suite,
  fitness, Home Assistant), if you want the "why," not just the "what."
