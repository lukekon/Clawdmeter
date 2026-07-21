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
$Watch = Join-Path $Repo "clawdmeter-watch.ps1"

$cmd = "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Watch`""
Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" `
    -Name "ClawdmeterWatch" -Value $cmd
Write-Host "Registered ClawdmeterWatch at logon (HKCU\Run, no admin)."

# Launch it now so you don't have to log out/in.
Start-Process powershell.exe `
    -ArgumentList "-NoProfile","-WindowStyle","Hidden","-ExecutionPolicy","Bypass","-File","`"$Watch`""
Write-Host "Watcher started. The daemon comes up whenever claude/grok/slate runs."
