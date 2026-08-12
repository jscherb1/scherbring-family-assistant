<#
.SYNOPSIS
One-time setup: registers a Windows Scheduled Task that runs the health watchdogs
every ~2 minutes.

.DESCRIPTION
Safe to re-run - replaces any existing task of the same name. This task no longer
drives the scheduled-tasks feature (that's a self-armed in-session CronCreate loop
now - see the "Scheduler self-arming" section of CLAUDE.md); it exists purely to
run the deterministic health backstops: watchdog_telegram_health.ps1 and
watchdog_scheduler_health.ps1. Adding a new scheduled task later is just a row in
the registry (via the `scheduler` subagent or scripts/scheduler_store.py directly)
and never touches Windows Task Scheduler.

If you're migrating from the older "PersonalAssistantSchedulerPoller" task, remove
it first:
    Unregister-ScheduledTask -TaskName PersonalAssistantSchedulerPoller -Confirm:$false

-MultipleInstances IgnoreNew prevents overlapping runs if a check pass ever takes
longer than the 2-minute interval. Run this once:

    powershell -ExecutionPolicy Bypass -File scripts\register_watchdog_task.ps1
#>

$TaskName = "PersonalAssistantWatchdog"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$WatchdogScript = Join-Path $RepoRoot "scripts\run_watchdog.ps1"
$HiddenLauncher = Join-Path $RepoRoot "scripts\run_watchdog_hidden.vbs"

# Routed through wscript.exe + a .vbs launcher rather than calling powershell.exe
# directly: `-WindowStyle Hidden` alone still flashes a console window briefly under
# Task Scheduler (Windows allocates the console before PowerShell applies the flag).
# See run_watchdog_hidden.vbs for details.
$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$HiddenLauncher`""

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 2) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -User $env:USERNAME `
    -Description "Runs the Telegram-connection and scheduler-loop health watchdogs every ~2 minutes." `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName'. It will start checking within 2 minutes."
Write-Host "To force an immediate check without waiting:"
Write-Host "  powershell -ExecutionPolicy Bypass -File `"$WatchdogScript`""
Write-Host "To remove it: Unregister-ScheduledTask -TaskName $TaskName"
