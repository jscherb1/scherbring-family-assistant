<#
.SYNOPSIS
One-time setup: registers a Windows Scheduled Task that runs the external scheduler
dispatcher every 2 minutes.

.DESCRIPTION
Safe to re-run — replaces any existing task of the same name. This task runs
scripts/scheduler_dispatch.py every 2 minutes. That script writes a heartbeat tick,
checks for due scheduled tasks, and exits immediately if nothing is due (zero Claude
API calls). When tasks are due it dispatches them via `claude --print` as one-shot
subprocesses, consuming tokens only for actual work.

This replaces the in-session CronCreate poll loop that previously ran inside the
orchestrator session. Moving the poll outside Claude eliminates ~720 idle turns/day
that were burning tokens even when nothing was scheduled.

The task is routed through wscript.exe + run_scheduler_hidden.vbs to avoid a
console-window flash on each 2-minute trigger (same technique as
register_watchdog_task.ps1).

The watchdog (watchdog_scheduler_health.ps1) monitors the heartbeat file written by
this task. If the heartbeat goes stale it will attempt to restart this task directly
rather than restarting the orchestrator.

Run once:
    powershell -ExecutionPolicy Bypass -File scripts\register_scheduler_task.ps1
#>

$TaskName = "PersonalAssistantScheduler"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$HiddenLauncher = Join-Path $RepoRoot "scripts\run_scheduler_hidden.vbs"

# Route through wscript.exe to suppress the console-window flash.
$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$HiddenLauncher`""

# Start immediately, repeat every 2 minutes for 10 years.
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 2) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 6)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -User $env:USERNAME `
    -Description "Runs scripts/scheduler_dispatch.py every 2 minutes. Fires Claude only when scheduled tasks are due; exits silently otherwise." `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName'. First dispatch check will run within 2 minutes."
Write-Host "To force an immediate dispatch check:"
Write-Host "  python `"$RepoRoot\scripts\scheduler_dispatch.py`""
Write-Host "To remove it: Unregister-ScheduledTask -TaskName $TaskName"
