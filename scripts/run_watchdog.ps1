<#
.SYNOPSIS
Runs one pass of the health watchdogs and logs it.

.DESCRIPTION
This is the action the "PersonalAssistantWatchdog" scheduled task (registered by
register_watchdog_task.ps1) invokes every ~2 minutes. It runs both health checks -
watchdog_telegram_health.ps1 (detects a stuck Telegram MCP connection) and
watchdog_scheduler_health.ps1 (detects a stalled self-armed scheduler poll loop) -
and appends their output to a per-day log file under state/logs/, so a check that
finds (or fixes) a problem leaves a trail instead of vanishing silently. Safe to run
by hand any time to force an immediate check.

(Historically this script also drove the scheduled-tasks poller itself via
scheduler_poll.py; that dispatch mechanism was retired in favor of a self-armed
in-session CronCreate loop - see the "Scheduler self-arming" section of CLAUDE.md.
This script is watchdog-only now.)
#>

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$LogDir = Join-Path $RepoRoot "state\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir ("watchdog_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $LogFile -Value "[$timestamp] --- watchdog pass ---"
& (Join-Path $PSScriptRoot "watchdog_telegram_health.ps1") 2>&1 | Tee-Object -FilePath $LogFile -Append
& (Join-Path $PSScriptRoot "watchdog_scheduler_health.ps1") 2>&1 | Tee-Object -FilePath $LogFile -Append
