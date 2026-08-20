<#
.SYNOPSIS
One-time setup: registers a Windows Scheduled Task that auto-starts the local
ha-mcp HTTP server (Home Assistant MCP) whenever you log on.

.DESCRIPTION
Safe to re-run - replaces any existing task of the same name. The task runs
start_ha_mcp.ps1 fully hidden (via run_ha_mcp_hidden.vbs, same technique as
register_scheduler_task.ps1), only while you're logged on. Run this once:

    powershell -ExecutionPolicy Bypass -File scripts\register_ha_mcp_task.ps1
#>

$TaskName = "PersonalAssistantHaMcp"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$HiddenLauncher = Join-Path $RepoRoot "scripts\run_ha_mcp_hidden.vbs"

$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$HiddenLauncher`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Auto-starts the local ha-mcp HTTP server (Home Assistant MCP, loopback-only) at logon." `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName'. It will start ha-mcp at your next logon."
Write-Host "To start it right now without logging off/on:"
Write-Host "  powershell -ExecutionPolicy Bypass -File `"$RepoRoot\scripts\start_ha_mcp.ps1`""
Write-Host "To remove it: Unregister-ScheduledTask -TaskName $TaskName"
