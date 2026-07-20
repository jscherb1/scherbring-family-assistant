<#
.SYNOPSIS
One-time setup: registers a Windows Scheduled Task that auto-starts the
personal-assistant orchestrator whenever you log on.

.DESCRIPTION
Safe to re-run - replaces any existing task of the same name. The task runs
start_orchestrator.ps1 in a minimized, visible window, only while you're logged on
(no Windows password is stored). Run this once:

    powershell -ExecutionPolicy Bypass -File scripts\register_orchestrator_task.ps1
#>

$TaskName = "PersonalAssistantOrchestrator"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$StartScript = Join-Path $RepoRoot "scripts\start_orchestrator.ps1"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Minimized -File `"$StartScript`""

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Auto-starts the personal-assistant orchestrator (Telegram channel) at logon." `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName'. It will start the orchestrator at your next logon."
Write-Host "To start it right now without logging off/on:"
Write-Host "  powershell -ExecutionPolicy Bypass -File `"$StartScript`""
