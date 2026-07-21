# clawdmeter-watch.ps1
# Run the Clawdmeter daemon ONLY while an AI CLI is active, so nothing heavy sits
# resident when you're not using Claude/Grok/Slate. This watcher is the single
# always-on piece, and it's featherweight: a Get-Process check every POLL_SEC.
# The daemon (BLE link + Anthropic poll + Grok log scan) runs solely on demand.
#
# Detection is by process name — claude/grok/slate are distinct native .exes, so
# this catches every launch method (terminal AND the VS Code extension, which
# spawns claude.exe just the same). Node is deliberately ignored.
#
# Register at login with register-watch.ps1 (or Task Scheduler). This REPLACES
# install-windows.ps1's always-on tray autostart — don't run both.

$ErrorActionPreference = "SilentlyContinue"

$Repo      = Split-Path -Parent $MyInvocation.MyCommand.Path
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
    $shouldRun = $active -or (((Get-Date) - $lastActive).TotalSeconds -lt $GRACE_SEC)

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
