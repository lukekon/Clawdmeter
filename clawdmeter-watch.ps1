# clawdmeter-watch.ps1
# Keep the Clawdmeter daemon resident (ALWAYS-ON) and restart it if it dies. The
# gauge is cabled full-time and the daemon also serves mouse side-button view
# control, so it no longer gates on AI activity. This watcher stays the single
# supervisor: featherweight, a liveness check every POLL_SEC.
#
# (History: it used to run the daemon only while claude/grok/slate were active;
# the AI-process detection below is retained for logging, but $shouldRun is now
# always true. Set it back to the gated form to restore on-demand behaviour.)
#
# Register at login with register-watch.ps1 (or Task Scheduler). This REPLACES
# install-windows.ps1's always-on tray autostart — don't run both.

$ErrorActionPreference = "SilentlyContinue"

$Repo      = Split-Path -Parent $MyInvocation.MyCommand.Path

# --- Windowless self-heal --------------------------------------------------
# However we were launched (Scheduled Task, HKCU\Run, manual), if we lack the
# CLAWD_HIDDEN marker we were started as a visible powershell.exe. When the
# default terminal is Windows Terminal, -WindowStyle Hidden is ignored and a
# PowerShell window lands on the taskbar. Re-launch ourselves windowless via the
# VBS shim (which sets CLAWD_HIDDEN) and exit, so any visible window closes.
if (-not $env:CLAWD_HIDDEN) {
    $vbs = Join-Path $Repo "clawdmeter-watch.vbs"
    Start-Process wscript.exe -ArgumentList "`"$vbs`"" -WindowStyle Hidden | Out-Null
    exit
}

# --- Single-instance lock --------------------------------------------------
# The self-heal relaunch detaches each hidden watcher from whatever launched it
# (Scheduled Task / Run entry), so the task's IgnoreNew dedup can't see it and a
# revive would stack a second watcher. A named mutex makes this a true singleton:
# the first hidden watcher holds it and runs; any later one can't acquire it and
# exits immediately. Global\ (not Local\) so it dedups across ALL sessions — the
# Scheduled Task and a Run-entry launch can land in different sessions. Held for
# the process lifetime via the script-scoped variable.
$script:WatchMutex = New-Object System.Threading.Mutex($false, "Global\ClawdmeterWatch")
try { $gotLock = $script:WatchMutex.WaitOne(0) }
catch [System.Threading.AbandonedMutexException] { $gotLock = $true }  # prior holder was killed
if (-not $gotLock) { exit }

$Daemon    = Join-Path $Repo "daemon\claude_usage_daemon_windows.py"
$SitePkgs  = Join-Path $Repo ".venv\Lib\site-packages"
$VenvPy    = Join-Path $Repo ".venv\Scripts\python.exe"
# BASE pythonw (not the venv's redirector stub, which pops a console); the daemon
# resolves its deps via PYTHONPATH below. Same rationale as install-windows.ps1.
$BasePrefix  = & $VenvPy -c "import sys; print(sys.base_exec_prefix)"
$BasePythonw = Join-Path $BasePrefix "pythonw.exe"

$AI_PROCS  = @("claude", "grok", "slate")
$POLL_SEC  = 10
$GRACE_SEC = 180   # keep the daemon up briefly between commands so back-to-back runs don't thrash the BLE link

$LogDir = Join-Path $env:LOCALAPPDATA "Clawdmeter\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
function Log($m) { "[$((Get-Date).ToString('o'))] $m" | Add-Content -Encoding UTF8 (Join-Path $LogDir "watch.log") }

# Adopt/clear any daemon this watcher didn't start (e.g. a previous watcher that
# was killed) so there's never more than one holding the BLE link.
function Get-DaemonProcs {
    Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
        Where-Object { $_.CommandLine -like "*claude_usage_daemon_windows.py*" }
}
function Stop-Daemon {
    foreach ($p in Get-DaemonProcs) { Stop-Process -Id $p.ProcessId -Force }
}
function Start-Daemon {
    $env:PYTHONPATH = $SitePkgs
    Start-Process -FilePath $BasePythonw -ArgumentList "`"$Daemon`"" -WorkingDirectory $Repo | Out-Null
}

Log "watcher start (poll ${POLL_SEC}s, grace ${GRACE_SEC}s)"
Stop-Daemon   # clean slate on (re)launch — kill strays, then let the loop decide

$lastActive = [DateTime]::MinValue
while ($true) {
    $active = $false
    foreach ($n in $AI_PROCS) { if (Get-Process -Name $n -ErrorAction SilentlyContinue) { $active = $true; break } }
    if ($active) { $lastActive = Get-Date }
    # Always-on: the desk gauge is cabled full-time and the daemon now also serves
    # mouse side-button view control, so keep it resident rather than gating on AI
    # activity. ($active/$GRACE_SEC kept above for logging/back-compat.)
    $shouldRun = $true

    $running = [bool](Get-DaemonProcs)
    if ($shouldRun -and -not $running) {
        Log "AI active -> starting daemon"
        Start-Daemon
    }
    elseif (-not $shouldRun -and $running) {
        # Force-kill, NOT a clean shutdown: skipping the daemon's GATT disconnect
        # leaves the bonded link up on the Windows side, so the device holds the
        # last-synced numbers (12h freshness) instead of blanking to its idle
        # screen. A clean Stop-Process would tell the device to let go.
        Log "idle past grace -> stopping daemon (device holds last numbers)"
        Stop-Daemon
    }
    Start-Sleep -Seconds $POLL_SEC
}
