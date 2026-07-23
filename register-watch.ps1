# register-watch.ps1 — run clawdmeter-watch.ps1 at every logon (per-user, NO admin).
# Uses HKCU\...\Run (same mechanism as install-windows.ps1), so no elevation.
# The watcher then runs the Clawdmeter daemon only while an AI CLI is active.
#
# Undo with:
#   reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v ClawdmeterWatch /f
#
# This REPLACES install-windows.ps1's always-on tray autostart. If you ran that,
# remove its entry too:
#   reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v Clawdmeter /f

$ErrorActionPreference = "Stop"
$Repo  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Vbs   = Join-Path $Repo "clawdmeter-watch.vbs"

# Launch via the windowless VBS shim (wscript). Running "powershell.exe
# -WindowStyle Hidden" directly still shows a taskbar window when the default
# terminal is Windows Terminal (WT ignores -WindowStyle); the shim never does.
$cmd = "wscript.exe `"$Vbs`""
Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" `
    -Name "ClawdmeterWatch" -Value $cmd
Write-Host "Registered ClawdmeterWatch at logon (HKCU\Run, no admin)."

# Launch it now, DETACHED. Win32_Process.Create spawns it under the WMI service,
# not as a child of this console — so closing this window (or the shell exiting)
# does NOT kill it. At logon the HKCU\Run entry launches it the same windowless way.
Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = "wscript.exe `"$Vbs`""
} | Out-Null
Write-Host "Watcher started (detached, windowless). The daemon comes up whenever claude/grok/slate runs."
