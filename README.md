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
| `.mcp.json` | Project MCP config (Todoist HTTP/OAuth). Gitignored. See `.mcp.json.example`. |
| `state/schema.sql` | SQLite schema for `agent_results` |
| `state/agent_results.db` | The state store (auto-created; gitignored — holds personal data) |
| `scripts/state_store.py` | Zero-dep CLI the subagent calls to write/read results |
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

## Launch (the orchestrator)

From the project root:

```
claude --channels plugin:telegram@claude-plugins-official
```

Leave this session running; it's the live orchestrator. Message your bot from Telegram.
(For always-on, run it in a persistent terminal window — Phase 2 will add scheduling.)

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

Cron/Task Scheduler, custom webhook channel, email delivery, subagents beyond Todoist,
containerization, Remote Control, notification-formatting skills.
