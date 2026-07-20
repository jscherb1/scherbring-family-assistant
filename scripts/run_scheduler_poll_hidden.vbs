' Launches run_scheduler_poll.ps1 with a fully hidden window.
'
' Task Scheduler + `powershell.exe -WindowStyle Hidden` is well known to still flash a
' console window briefly, because Windows allocates a console for the process before
' PowerShell gets to apply its own hidden-window argument. Routing through wscript.exe's
' WScript.Shell.Run with windowStyle=0 avoids that entirely - this is the standard
' workaround. See register_scheduler_poller_task.ps1, which points the scheduled task's
' action at this file instead of powershell.exe directly.

Dim fso, scriptDir, shell
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

Set shell = CreateObject("WScript.Shell")
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & scriptDir & "\run_scheduler_poll.ps1""", 0, False
