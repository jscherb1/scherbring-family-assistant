<#
.SYNOPSIS
Runs one pass of the scheduled-tasks poller and logs it.

.DESCRIPTION
This is the action the "PersonalAssistantSchedulerPoller" scheduled task (registered
by register_scheduler_poller_task.ps1) invokes every ~2 minutes. It just runs
scheduler_poll.py and appends its output to a per-day log file under state/logs/, so
a poll pass that fails (e.g. the orchestrator/channel isn't listening) leaves a trail
instead of vanishing silently. Safe to run by hand any time to force an immediate
poll pass.
#>

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$LogDir = Join-Path $RepoRoot "state\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir ("scheduler_poll_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $LogFile -Value "[$timestamp] --- poll pass ---"
python scripts\scheduler_poll.py 2>&1 | Tee-Object -FilePath $LogFile -Append

# Piggyback the Telegram MCP health check on this same 2-minute cadence so a
# broken connection (see watchdog_telegram_health.ps1 for background) gets
# detected and healed in minutes rather than sitting silently for hours.
& (Join-Path $PSScriptRoot "watchdog_telegram_health.ps1") 2>&1 | Tee-Object -FilePath $LogFile -Append
