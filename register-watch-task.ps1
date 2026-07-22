# register-watch-task.ps1 — run clawdmeter-watch.ps1 via a Scheduled Task that
# both starts it at logon AND revives it if it dies mid-session.
#
# Why a task and not just HKCU\Run: the Run entry fires ONLY at logon, so if the
# watcher process ever dies while you're logged in (crash, closed host shell, etc.)
# the daemon stops coming up until your next login. This task adds a 5-minute
# repetition with IgnoreNew, so a dead watcher is respawned within 5 min, while a
# live one is never doubled. No admin needed — it's a per-user task.
#
# Undo:  Unregister-ScheduledTask -TaskName ClawdmeterWatch -Confirm:$false

$ErrorActionPreference = "Stop"
$Repo  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Watch = Join-Path $Repo "clawdmeter-watch.ps1"
$TaskName = "ClawdmeterWatch"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Watch`""

# At-logon trigger, plus a 5-min repetition running indefinitely so a mid-session
# death is recovered without waiting for the next logon.
$trigger = New-ScheduledTaskTrigger -AtLogOn
$rep = (New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Minutes 5)).Repetition
$trigger.Repetition = $rep

# IgnoreNew = never start a 2nd instance if one is already running (dedupe).
# ExecutionTimeLimit 0 = never time-limit it (the watcher is an infinite loop).
# RestartCount/Interval = belt-and-suspenders if it exits as failed.
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

# Interactive (run only when logged on) — BLE + the user session are required.
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force | Out-Null
} catch {
    Write-Warning "Register-ScheduledTask failed: $($_.Exception.Message)"
    Write-Warning "Re-run this from an ELEVATED PowerShell (Run as administrator). Your existing HKCU\Run logon launcher was left untouched."
    exit 1
}
Write-Host "Registered scheduled task '$TaskName' (logon + 5-min revive)."

# Only now that the task is in place, retire the old HKCU\Run launcher so we don't
# get two watchers fighting over the BLE link (each force-kills strays on start).
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" `
    -Name "ClawdmeterWatch" -ErrorAction SilentlyContinue

# Kill any watcher already running (excluding nothing — this shell isn't the watcher),
# so the task's single managed instance is the only one.
Get-CimInstance Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'" |
    Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -like '*clawdmeter-watch.ps1*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

# Kick it off now so we don't wait for the first logon/repetition.
Start-ScheduledTask -TaskName $TaskName
Write-Host "Started now. The daemon comes up whenever claude/grok/slate runs."
