' clawdmeter-watch.vbs — launch the Clawdmeter watcher with ZERO visible window.
'
' Why this shim exists: launching "powershell.exe -WindowStyle Hidden" directly
' still pops a taskbar window when the user's default terminal is Windows
' Terminal (WT ignores -WindowStyle). wscript.exe is itself windowless, and
' Run(cmd, 0, False) creates the child with SW_HIDE, so no console — and thus no
' WT tab — ever appears. The watcher (an infinite Get-Process poll loop) then
' runs as a true background process.
'
' Used by both register-watch.ps1 (HKCU\Run) and register-watch-task.ps1
' (Scheduled Task) so every launch path is windowless.

Dim shell, repo, ps
Set shell = CreateObject("WScript.Shell")
' This .vbs lives in the repo root; resolve the watcher next to it.
repo = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
' Mark the child so the watcher's self-heal guard knows it's already windowless
' (inherited by the process Run() spawns) and doesn't relaunch itself again.
shell.Environment("PROCESS").Item("CLAWD_HIDDEN") = "1"
ps = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & repo & "clawdmeter-watch.ps1"""
' Window style 0 = hidden. Wait = True so, when the Scheduled Task launches this
' shim, wscript stays alive for the watcher's lifetime — the task instance reads
' as "running" (IgnoreNew suppresses the 5-min repetition) yet still revives if
' the watcher ever dies. wscript itself is windowless, so nothing shows.
shell.Run ps, 0, True
