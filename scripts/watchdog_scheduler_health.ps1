<#
.SYNOPSIS
Detects a stalled external scheduler dispatch task and recovers it, then alerts
directly over Telegram - independent of the orchestrator session or any MCP
connection.

.DESCRIPTION
The scheduled-tasks dispatcher is scripts/scheduler_dispatch.py, run every 2 minutes
by the PersonalAssistantScheduler Windows Task Scheduler task (registered by
register_scheduler_task.ps1). On each run it writes state/scheduler_loop_state.json
`last_tick`. This script checks whether that tick is fresh. If `last_tick` goes stale
beyond a threshold, the Task Scheduler job has likely stopped running.

Note: the scheduler is now independent of the orchestrator. A stale heartbeat does
NOT mean the orchestrator is broken - it means the PersonalAssistantScheduler task
needs attention. Recovery targets that task directly rather than restarting the
orchestrator.

To avoid acting on a single transient blip, an unhealthy reading must persist across
two checks at least 5 minutes apart (tracked in state/scheduler_watchdog.json)
before this script acts.

On confirmed persistent staleness:
  1. Run scheduler_dispatch.py once directly to immediately restore the heartbeat.
  2. Trigger the PersonalAssistantScheduler task and re-enable it if disabled.
  3. Send a direct alert to Telegram via the Bot API (no Claude/MCP dependency).

Meant to be called every ~2 minutes via run_watchdog.ps1 / PersonalAssistantWatchdog.
#>

$RepoRoot = Split-Path -Parent $PSScriptRoot
$StateFile = Join-Path $RepoRoot "state\scheduler_watchdog.json"
$LoopStateFile = Join-Path $RepoRoot "state\scheduler_loop_state.json"
$ConfigFile = Join-Path $RepoRoot "scripts\scheduler.config.json"
$TelegramEnvFile = Join-Path $env:USERPROFILE ".claude\channels\telegram\.env"
$DispatchScript = Join-Path $RepoRoot "scripts\scheduler_dispatch.py"
$SchedulerTaskName = "PersonalAssistantScheduler"
$StaleThresholdMinutes = 20
$DebounceMinutes = 5

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

$now = Get-Date

$isUnhealthy = $false
$reason = $null

if (-not (Test-Path $LoopStateFile)) {
    # Grace period: the task runs every 2 min so it should exist within 4 minutes
    # of the task being registered. Only flag as unhealthy after 5 minutes.
    $isUnhealthy = $true
    $reason = "scheduler heartbeat file missing (state/scheduler_loop_state.json not found)"
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
        Write-Host "watchdog: persistent scheduler stall confirmed ($reason) - recovering"

        # Run dispatch script directly once to immediately restore the heartbeat
        # and fire any overdue tasks.
        try {
            & python $DispatchScript 2>&1 | Out-Null
            Write-Host "watchdog: ran scheduler_dispatch.py directly"
        }
        catch {
            Write-Host "watchdog: failed to run scheduler_dispatch.py directly: $_"
        }

        # Re-enable and trigger the Task Scheduler task if it exists.
        try {
            $task = Get-ScheduledTask -TaskName $SchedulerTaskName -ErrorAction SilentlyContinue
            if ($task) {
                if ($task.State -eq 'Disabled') {
                    Enable-ScheduledTask -TaskName $SchedulerTaskName | Out-Null
                    Write-Host "watchdog: re-enabled $SchedulerTaskName task"
                }
                Start-ScheduledTask -TaskName $SchedulerTaskName -ErrorAction SilentlyContinue
                Write-Host "watchdog: triggered $SchedulerTaskName task"
            }
            else {
                Write-Host "watchdog: $SchedulerTaskName task not found - run scripts\register_scheduler_task.ps1"
            }
        }
        catch {
            Write-Host "watchdog: error managing $SchedulerTaskName task: $_"
        }

        $state.first_unhealthy_at = $null
        $state.last_restart_at = $now.ToString("o")
        Save-WatchdogState $state

        # Only alert if the Task Scheduler task itself ran recently but last_tick is
        # still stale - that's a real bug in the dispatch script. If the task also
        # hasn't run recently (same staleness), the machine was simply asleep; recover
        # silently so the user isn't woken up after every sleep cycle.
        $taskInfo = Get-ScheduledTaskInfo -TaskName $SchedulerTaskName -ErrorAction SilentlyContinue
        $taskAlsoStale = (-not $taskInfo) -or (($now - $taskInfo.LastRunTime).TotalMinutes -ge $StaleThresholdMinutes)
        if (-not $taskAlsoStale) {
            Send-TelegramAlert "⚠️ Scheduler dispatch stalled ($reason). External task was triggered to recover."
        }
        else {
            Write-Host "watchdog: stale tick correlates with stale task (machine was likely asleep) - recovered silently"
        }
    }
}
else {
    if ($state.first_unhealthy_at) {
        Write-Host "watchdog: scheduler poll loop recovered on its own - clearing tracking"
        $state.first_unhealthy_at = $null
        Save-WatchdogState $state
    }
}
