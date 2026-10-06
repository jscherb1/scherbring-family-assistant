# Opens a visible window showing the assistant's tmux session every time you log in to Windows.
# Run once in PowerShell (no admin needed):  powershell -ExecutionPolicy Bypass -File <this file>
# Remove later with:  Unregister-ScheduledTask -TaskName WSL-Assistant-Window -Confirm:$false
# The assistant runs regardless of this window; closing it only detaches the view.
param(
    [string]$Distro = "Ubuntu-24.04",
    [string]$User = "jscherb1",
    [string]$RepoRoot = "~/projects/scherbring-family-assistant"
)
$action = New-ScheduledTaskAction -Execute "wsl.exe" `
    -Argument "-d $Distro -u $User -- bash $RepoRoot/scripts/attach_orchestrator.sh"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "WSL-Assistant-Window" -Action $action -Trigger $trigger -Settings $settings `
    -Description "Show the family assistant tmux session at logon" -Force
Write-Host "Registered. Start it now with: Start-ScheduledTask WSL-Assistant-Window"
