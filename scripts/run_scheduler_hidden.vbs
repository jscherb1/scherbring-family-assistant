' Launches scheduler_dispatch.py with a fully hidden window.
'
' Task Scheduler + `python.exe -WindowStyle Hidden` still flashes a console window
' briefly. Routing through wscript.exe's WScript.Shell.Run with windowStyle=0 avoids
' that. Same pattern as run_watchdog_hidden.vbs. See register_scheduler_task.ps1.

Dim fso, scriptDir, repoRoot, shell
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
repoRoot = fso.GetParentFolderName(scriptDir)

Set shell = CreateObject("WScript.Shell")
shell.Run "python """ & repoRoot & "\scripts\scheduler_dispatch.py""", 0, False
