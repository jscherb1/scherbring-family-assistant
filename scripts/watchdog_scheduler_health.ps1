<#
.SYNOPSIS
Detects a stalled scheduled-tasks poll loop inside the running orchestrator and
force-restarts it so start_orchestrator.ps1's loop (with --continue) can bring it
back up, then alerts directly over Telegram - independent of the orchestrator
session or any MCP connection.

.DESCRIPTION
Background: the scheduled-tasks poll loop (see the "Scheduler self-arming" section
of CLAUDE.md) is a self-armed, session-only Claude Code CronCreate job - the
orchestrator arms it in-session and there is no external process driving it. That
makes it a "soft" mechanism: if the model ever fails to re-arm it (a dropped
instruction, a context issue, a bug), nothing else would notice until a scheduled
task silently failed to fire. This script is the deterministic backstop: it does
not trust the orchestrator's word for it, and needs no AI cooperation to detect a
stalled loop or to recover from one.

Detection: the self-armed loop writes state/scheduler_loop_state.json, updating its
`last_tick` field every time it fires (~2 min cadence) and `armed_at`/`job_id`
whenever it (re-)arms. If the orchestrator process is running but `last_tick` is
stale beyond a tolerant threshold, that's a live process with a dead internal loop -
a failure mode invisible to a simple process-is-running check. A missing state file
after the process has had a few minutes to arm it counts the same way (the loop
never armed in the first place).

To avoid restarting on a single transient blip (e.g. a slow turn pushing one tick a
few minutes late), an unhealthy reading must persist across two checks at least 3
minutes apart (tracked in state/scheduler_watchdog.json) before this script acts,
mirroring watchdog_telegram_health.ps1's debounce pattern.

On confirmed persistent staleness:
  1. Force-kill the orchestrator so start_orchestrator.ps1's wrapper restarts it
     with --continue, giving the CLAUDE.md re-arm check a fresh shot on the next turn.
  2. Send a direct alert straight to the Telegram Bot API using the bot token from
     ~/.claude/channels/telegram/.env and the alert_chat_id from
     scripts/scheduler.config.json - this never goes through Claude Code or any MCP
     connection, so it still gets through even if the orchestrator was fully wedged.

Meant to be called every ~2 minutes (piggybacked on the same Task Scheduler cadence
as watchdog_telegram_health.ps1 via run_watchdog.ps1).
#>

$RepoRoot = Split-Path -Parent $PSScriptRoot
$StateFile = Join-Path $RepoRoot "state\scheduler_watchdog.json"
$LoopStateFile = Join-Path $RepoRoot "state\scheduler_loop_state.json"
$ConfigFile = Join-Path $RepoRoot "scripts\scheduler.config.json"
$TelegramEnvFile = Join-Path $env:USERPROFILE ".claude\channels\telegram\.env"
$MatchPattern = '*channels*plugin:telegram*'
$StaleThresholdMinutes = 10
$ArmGraceMinutes = 5
$DebounceMinutes = 3

function Get-WatchdogState {
    if (Test-Path $StateFile) {
        try { return Get-Content $StateFile -Raw | ConvertFrom-Json } catch { }
    }
    return [PSCustomObject]@{ first_unhealthy_at = $null; last_restart_at = $null }
}

function Save-WatchdogState($state) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $StateFile) | Out-Null
    $state | ConvertTo-Json | Set-Content -Path $StateFile -Encoding utf8
}

function Send-TelegramAlert([string]$Text) {
    try {
        if (-not (Test-Path $TelegramEnvFile) -or -not (Test-Path $ConfigFile)) { return }

        $token = $null
        foreach ($line in Get-Content $TelegramEnvFile) {
            if ($line -match '^TELEGRAM_BOT_TOKEN=(.+)$') { $token = $matches[1].Trim() }
        }
        if (-not $token) { return }

        $config = Get-Content $ConfigFile -Raw | ConvertFrom-Json
        $chatId = $config.alert_chat_id
        if (-not $chatId) { return }

        $uri = "https://api.telegram.org/bot$token/sendMessage"
        Invoke-RestMethod -Uri $uri -Method Post -Body @{ chat_id = $chatId; text = $Text } -ErrorAction Stop | Out-Null
    }
    catch {
        Write-Host "watchdog: failed to send direct Telegram alert: $_"
    }
}

# Nothing to heal if the orchestrator isn't even running - process-level restart is
# already start_orchestrator.ps1's job.
$proc = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like $MatchPattern } | Select-Object -First 1
if (-not $proc) {
    exit 0
}

$now = Get-Date
$procAgeMinutes = ($now - $proc.CreationDate).TotalMinutes

$isUnhealthy = $false
$reason = $null

if (-not (Test-Path $LoopStateFile)) {
    if ($procAgeMinutes -ge $ArmGraceMinutes) {
        $isUnhealthy = $true
        $reason = "scheduler poll loop never armed (no state/scheduler_loop_state.json $ArmGraceMinutes+ min after orchestrator start)"
    }
}
else {
    try {
        $loopState = Get-Content $LoopStateFile -Raw | ConvertFrom-Json
        $lastTick = [datetime]$loopState.last_tick
        $tickAgeMinutes = ($now - $lastTick).TotalMinutes
        if ($tickAgeMinutes -ge $StaleThresholdMinutes) {
            $isUnhealthy = $true
            $reason = "scheduler poll loop stale (last tick $([math]::Round($tickAgeMinutes, 1)) min ago)"
        }
    }
    catch {
        # Malformed state file - treat as unknown, not unhealthy. Absence/corruption
        # of data never triggers a restart on its own.
    }
}

$state = Get-WatchdogState

if ($isUnhealthy) {
    if (-not $state.first_unhealthy_at) {
        $state.first_unhealthy_at = $now.ToString("o")
        Save-WatchdogState $state
        Write-Host "watchdog: $reason - starting $DebounceMinutes-minute confirmation window"
        exit 0
    }

    $firstSeen = [datetime]$state.first_unhealthy_at
    if (($now - $firstSeen).TotalMinutes -ge $DebounceMinutes) {
        Write-Host "watchdog: persistent scheduler-loop stall confirmed ($reason) - restarting orchestrator (pid $($proc.ProcessId))"
        Stop-Process -Id $proc.ProcessId -Force
        $state.first_unhealthy_at = $null
        $state.last_restart_at = $now.ToString("o")
        Save-WatchdogState $state
        Send-TelegramAlert "⚠️ Scheduler poll loop went stale ($reason). Orchestrator was force-restarted to recover."
    }
}
else {
    if ($state.first_unhealthy_at) {
        Write-Host "watchdog: scheduler poll loop recovered on its own - clearing tracking"
        $state.first_unhealthy_at = $null
        Save-WatchdogState $state
    }
}
