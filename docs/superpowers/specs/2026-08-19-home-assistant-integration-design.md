# Home Assistant integration

## Context

Before this work, no subagent, skill, or file in the repo touched Home Assistant —
the only mention was a README backlog bullet noting the hosted Home Assistant MCP
connector was "connected but unused." That connector
(`mcp__claude_ai_Home_Assistant__*`) exposes simple actuation (`Hass*` tools) and a
read-only live-state snapshot (`GetLiveContext`), but no automation/script/dashboard
CRUD and no general entity/history query.

The user wanted three things, in decreasing frequency: (1) ad-hoc chat control,
(2) authoring/editing HA automations and scripts, and (3) proactive
monitoring/alerts — plus, longer-term, HA state generally available as ambient
context to other subagents (no specific first use case chosen, so kept light-touch).
They confirmed they could generate an HA long-lived access token, unlocking (2) and
richer state access via HA's REST/WebSocket API — the same shape of problem the repo
already solved once for Monarch Money (official MCP paused → vendor a local MCP
server). This design reuses that pattern for HA.

## Approach

One subagent, not two. `.claude/agents/home-assistant.md` owns the whole HA domain —
ad-hoc control and automation authoring are the same mental bucket for the user, and
splitting them would just create an orchestrator routing problem. Its tool allowlist
and capabilities grow across phases as more of HA's API becomes available.

## Phase 1 — Ad-hoc control (built)

`.claude/agents/home-assistant.md`, scoped to the hosted `Hass*` tools + `GetLiveContext`
+ `GetDateTime`. No new setup required. Security-sensitive actuators (locks, garage
doors, door/garage-classed covers) require explicit confirmation before acting, even
on direct ad-hoc chat requests — stricter than most other subagents in this repo,
which only gate proactive writes.

## Phase 2 — Local `ha` MCP server (built)

Evaluated several open-source Home Assistant MCP servers
([`homeassistant-ai/ha-mcp`](https://github.com/homeassistant-ai/ha-mcp),
`zorak1103/ha-mcp`, `ganhammar/hass-mcp-server`, `voska/hass-mcp`,
`robbrad/homeassistant-mcp`) and chose `homeassistant-ai/ha-mcp`: Python, MIT
license, actively maintained (2,371+ commits), 88 tools spanning automations,
scripts, scenes, helpers, dashboards, areas/zones/labels, history, and traces.

Upstream explicitly discourages the stdio transport used by the repo's existing
Monarch vendoring pattern ("known transport issues... recommended only for
demo/testing"), and warns against running two connection methods for the same
server simultaneously. Given that guidance, the user chose the **local HTTP server**
mode over local-stdio or installing it as a Home Assistant HACS custom component.

Implementation:
- Installed the `ha-mcp` PyPI package into `vendor/ha-mcp/.venv` (`pip install
  ha-mcp` — no git clone needed, unlike Monarch, since it's a published package).
- `HOMEASSISTANT_URL` / `HOMEASSISTANT_TOKEN` stored in the gitignored `.env`
  (matching the existing Hy-Vee credential pattern in this repo — plaintext but
  never committed — rather than introducing a new keyring-based flow for a single
  token pair).
- `scripts/start_ha_mcp.ps1` binds loopback-only (`MCP_HOST=127.0.0.1`, default port
  8086, default `/mcp` path) so the default path doesn't need a high-entropy secret
  per upstream's threat model (`_warn_if_default_path_exposed` in `ha_mcp/__main__.py`).
  It's idempotent (checks `Get-Process -Name ha-mcp-web` before starting) and
  restarts on crash.
- `scripts/register_ha_mcp_task.ps1` + `scripts/run_ha_mcp_hidden.vbs` register an
  `AtLogOn` Scheduled Task (`PersonalAssistantHaMcp`), mirroring
  `register_orchestrator_task.ps1`.
- Registered as `ha` (`type: http`, `url: http://127.0.0.1:8086/mcp`) in `.mcp.json`
  (gitignored) and `.mcp.json.example` (template, no secret — the URL itself carries
  no token since the bind is loopback-only).

**Known gotcha (fixed during implementation):** the first version of
`start_ha_mcp.ps1`'s "already running" check used
`Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*ha-mcp-web*' }`,
which self-matched against the *querying* process's own command line whenever that
literal string appeared in the invoking command (e.g. ad-hoc diagnostic queries run
through a `-Command` wrapper). Replaced with `Get-Process -Name "ha-mcp-web"`, which
checks the process image name instead of command-line text and doesn't have this
failure mode.

## Phase 3 — Automation/script/scene/helper/dashboard authoring (built)

Added to `.claude/agents/home-assistant.md`: read-before-write (check for name
collisions / fetch current definition before editing), propose-then-confirm (plain-
language summary of trigger/condition/action before any `ha_config_set_*` call),
never silently overwrite, and a stricter confirmation bar for automations touching
security-sensitive entities. History/trace tools (`ha_get_automation_traces`,
`ha_get_logs`, `ha_get_history`) support "why didn't X run" debugging.

The subagent's `mcp__ha__*` tool allowlist is deliberately scoped to control/query,
automation/script/scene/helper/dashboard CRUD, and history/debugging — it excludes
higher-blast-radius tools the upstream server also exposes (backups, updates, HACS
management, device/entity registry removal, YAML/file editing, security policy,
Matter radio management). None of those were asked for, and they're out of scope
until specifically needed.

## Phase 4 — Proactive monitoring (not yet built)

Planned: a presence-based dedup table (`ha_alerts` in `state/schema.sql`, unlike the
date-based `weather_alerts` — an "open garage door" alert should re-arm once the
condition clears and recurs, not just once per day) and a `scripts/ha_store.py` CLI
(`alert log/check/clear/list`), following the `weather-reminders`/`home-maintenance`
proactive-check shape: user picks what to monitor, checks run through the existing
generic `scheduler` engine, stay silent when nothing's new, post one consolidated
message per firing, and never act automatically — propose only.

## Phase 5 — Ambient HA context for other agents (not yet built)

Planned as documentation-only: note that any subagent needing current
presence/sensor/device state can be granted a read-only `mcp__ha__*` or
`GetLiveContext` tool directly, or delegate a one-off question to `home-assistant`.
No shared cache, no polling, no new subsystem — the user explicitly chose "general
ambient context" over a specific first use case, so this stays minimal until a
concrete need (e.g. a combined "goodnight" or "leaving for school" routine) comes up.

## Security/guardrail summary

| Category | Ad-hoc chat | Proactive/scheduled |
|---|---|---|
| Lights, media, climate (normal range) | Execute directly, confirm after | not monitored |
| Locks, garage doors, covers | Confirm before acting, always | never act — propose only (Phase 4) |
| Automation/script/scene/dashboard create or edit | Confirm before writing, always | n/a |
| New alert condition detected | n/a | silent when nothing new; one message when triggered (Phase 4) |

Credential handling: HA long-lived token stored in the gitignored `.env`, never
committed; `.mcp.json` (also gitignored) holds only the loopback URL, no token.
