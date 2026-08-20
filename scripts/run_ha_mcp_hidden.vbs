' Launches start_ha_mcp.ps1 with a fully hidden window.
'
' Task Scheduler + `powershell.exe -WindowStyle Hidden` still flashes a console
' window briefly. Routing through wscript.exe's WScript.Shell.Run with
' windowStyle=0 avoids that. Same pattern as run_scheduler_hidden.vbs. See
' register_ha_mcp_task.ps1.

Dim fso, scriptDir, repoRoot, shell, startScript
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
repoRoot = fso.GetParentFolderName(scriptDir)
startScript = repoRoot & "\scripts\start_ha_mcp.ps1"

Set shell = CreateObject("WScript.Shell")
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & startScript & """", 0, False
