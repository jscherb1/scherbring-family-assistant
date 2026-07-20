<#
.SYNOPSIS
One-time setup: registers a Windows Scheduled Task that fires the scheduled-tasks
poller every ~2 minutes.

.DESCRIPTION
Safe to re-run - replaces any existing task of the same name. This is the ONLY
Scheduled Task the scheduled-tasks feature needs; adding a new scheduled task later
is just a row in the registry (via the `scheduler` subagent or
scripts/scheduler_store.py directly) and never touches Windows Task Scheduler again.

-MultipleInstances IgnoreNew prevents overlapping runs if a poll pass ever takes
longer than the 2-minute interval. Run this once:

    powershell -ExecutionPolicy Bypass -File scripts\register_scheduler_poller_task.ps1
#>

$TaskName = "PersonalAssistantSchedulerPoller"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$PollScript = Join-Path $RepoRoot "scripts\run_scheduler_poll.ps1"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PollScript`""

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
    -Description "Fires due scheduled tasks (scripts/scheduler_poll.py) every ~2 minutes." `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName'. It will start polling within 2 minutes."
Write-Host "To force an immediate poll pass without waiting:"
Write-Host "  powershell -ExecutionPolicy Bypass -File `"$PollScript`""
Write-Host "To remove it: Unregister-ScheduledTask -TaskName $TaskName"
