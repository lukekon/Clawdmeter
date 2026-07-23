#!/usr/bin/env python3
"""Claude Usage Tracker Daemon — Windows (Phase 2).

Reads the Claude OAuth token from the native-Windows credentials path and
polls the Anthropic API for rate-limit utilization data. BLE glue added in
later plans.
"""

import asyncio
import calendar
import datetime
import json
import logging
import logging.handlers
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

DEVICE_NAME = "Clawdmeter"
SERVICE_UUID = "4c41555a-4465-7669-6365-000000000001"
RX_CHAR_UUID = "4c41555a-4465-7669-6365-000000000002"
REQ_CHAR_UUID = "4c41555a-4465-7669-6365-000000000004"

POLL_INTERVAL = 60
TICK = 5
SCAN_TIMEOUT = 8.0         # seconds to scan for the advertising device (not-yet-OS-paired)
CONNECT_RETRIES = 3        # D-01: attempts before giving up on a device
CONNECT_RETRY_DELAY = 2.0  # D-01: seconds between failed connect attempts
ZOMBIE_BREAK_LIMIT = 1     # D-03: consecutive write failures before abandoning a half-open link
                           # N=1: breaks at T=60s, leaves ~60s headroom for reconnect+poll inside 120s SLA
                           # N=2 would bust the 120s budget before reconnect even begins
RECONNECT_BACKOFF_CAP = 8  # D-05: fast-reconnect cap (seconds); keeps stacked retries inside 120s SLA
                           # ~5–10s band per CONTEXT.md Claude's Discretion; 8 chosen as middle ground

# Optional reset chime.
# Optional clock display. 
# Config lives under the same Clawdmeter dir as daemon.log.
CONFIG_FILE = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Clawdmeter" / "config"

API_URL = "https://api.anthropic.com/v1/messages"
API_HEADERS_TEMPLATE = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "Content-Type": "application/json",
    "User-Agent": "claude-code/2.1.5",
}
API_BODY = {
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 1,
    "messages": [{"role": "user", "content": "hi"}],
}

# Grok CLI + Slate activity, computed HERE from the local logs so the device
# needs only this daemon running — not PitCrew too. The math mirrors PitCrew's
# AI Usage tab (same rate cards, same cached-token-subset handling) so the two
# agree, but nothing has to be up for the device to show Grok.
GROK_SESSIONS = Path.home() / ".grok" / "sessions"
SLATE_MESSAGES = Path.home() / ".local" / "share" / "slate" / "storage" / "message"
# $ per 1M tokens (input, output, cacheRead). xAI/Grok only — the device's Grok
# view is xAI. Verified against docs.x.ai pricing; keep in sync with PitCrew's
# lib/usage-rates.ts. Longest prefix first so grok-4.5 wins over any grok-4.
XAI_RATES = [
    ("grok-4.5", (2.0, 6.0, 0.5)),
    ("grok-4.3", (1.25, 2.5, 0.2)),
]
GROK_RECOMPUTE_S = 300  # a desk gauge needn't be fresher than this; keeps the scan cheap
_grok_cache = {"ts": 0.0, "week": 0.0, "today": 0.0, "wpct": 0, "dpct": 0, "wreset": -1}
# The % bars come from xAI's OWN weekly limit, not a made-up $ budget. The Grok CLI
# logs every billing refresh to unified.jsonl ("billing: fetched credits config" →
# creditUsagePercent) — the exact number it prints as "Weekly limit: N%". We read that
# straight. Grok exposes only a weekly limit (no session/daily one like Claude), so the
# "today" bar is derived: how much of that weekly % was consumed since local midnight.
GROK_LOG = Path.home() / ".grok" / "logs" / "unified.jsonl"


def _grok_cost(model: str, fresh: int, output: int, cached: int) -> float:
    """$ at API rates for one xAI turn, or 0 for a non-xAI/unknown model. Cached
    reads are billed at the cheaper cacheRead rate; `fresh` is already the
    non-cached input (see the subset note in _recompute_grok)."""
    for prefix, (ci, co, cc) in XAI_RATES:
        if model.startswith(prefix):
            return (fresh * ci + output * co + cached * cc) / 1_000_000.0
    return 0.0


def _recompute_grok() -> tuple[float, float]:
    """Scan the local Grok logs for this week's and today's xAI $ activity.

    Two sources, same as PitCrew: the Grok CLI (~/.grok/sessions/*/*/updates.jsonl,
    usage on `turn_completed` records) and Slate (~/.local/share/slate/storage/
    message/*/msg_*.json, one assistant message each). In BOTH, cachedReadTokens
    is a SUBSET of input, so fresh input = input - cached (billing it as-is would
    massively overstate cost at a high cache rate). Files whose mtime predates the
    week are skipped whole — their records are all older than the window."""
    now = time.time()
    week_cut = now - 7 * 86400
    lt = time.localtime(now)
    today_cut = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    week = today = 0.0

    def add(cost: float, ts: float) -> None:
        nonlocal week, today
        if cost <= 0:
            return
        if ts >= week_cut:
            week += cost
        if ts >= today_cut:
            today += cost

    # Grok CLI — turn_completed carries a modelUsage map; timestamp is unix seconds.
    try:
        cli_files = list(GROK_SESSIONS.glob("*/*/updates.jsonl"))
    except OSError:
        cli_files = []
    for f in cli_files:
        try:
            if f.stat().st_mtime < week_cut:
                continue
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "turn_completed" not in line:  # cheap prefilter over a big log
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    up = (rec.get("params") or {}).get("update") or {}
                    if up.get("sessionUpdate") != "turn_completed":
                        continue
                    ts = rec.get("timestamp")
                    per_model = (up.get("usage") or {}).get("modelUsage") or {}
                    if not ts or not per_model:
                        continue
                    for model, m in per_model.items():
                        cached = m.get("cachedReadTokens") or 0
                        fresh = max(0, (m.get("inputTokens") or 0) - cached)
                        add(_grok_cost(model, fresh, m.get("outputTokens") or 0, cached), ts)
        except OSError:
            continue

    # Slate — one JSON per message; model is "provider/model"; timestamp is unix ms.
    try:
        slate_files = list(SLATE_MESSAGES.glob("*/msg_*.json"))
    except OSError:
        slate_files = []
    for f in slate_files:
        try:
            if f.stat().st_mtime < week_cut:
                continue
            rec = json.loads(f.read_text(encoding="utf-8"))
            model = rec.get("model") or ""
            if rec.get("role") != "assistant" or not model.startswith("xai/"):
                continue
            usage = rec.get("usage") or {}
            cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
            fresh = max(0, (usage.get("prompt_tokens") or 0) - cached)
            cost = _grok_cost(model.split("/", 1)[1], fresh, usage.get("completion_tokens") or 0, cached)
            add(cost, (rec.get("timestamp") or 0) / 1000.0)
        except (OSError, ValueError):
            continue

    return week, today


def _iso_to_epoch(s) -> float | None:
    """'2026-07-22T00:25:59.541Z' → unix seconds, or None if unparseable."""
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _mins_until(epoch: float | None) -> int:
    """Whole minutes from now until `epoch`, floored at 0; -1 if unknown."""
    if not epoch:
        return -1
    return max(0, int((epoch - time.time()) // 60))


def read_grok_limit() -> tuple[int, int, int]:
    """xAI's real weekly-limit utilisation, read from the Grok CLI's own billing log.

    The CLI logs a `billing: fetched credits config` line carrying `creditUsagePercent`
    every time it refreshes — the exact figure it shows as "Weekly limit: N%". We take
    the latest reading as the weekly %, and derive a "today" % by diffing it against the
    last reading before local midnight in the SAME billing period (Grok has no daily
    limit of its own). Returns (week_pct, today_pct, week_reset_mins); 0/0/-1 if the log
    is absent or unreadable, so the bars just go empty rather than show an invented value."""
    try:
        readings = []  # (ts_epoch, pct, period_start, period_end)
        with open(GROK_LOG, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "billing: fetched credits config" not in line:  # cheap prefilter
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                cfg = ((rec.get("ctx") or {}).get("config")) or {}
                pct = cfg.get("creditUsagePercent")
                if pct is None:
                    continue
                per = cfg.get("currentPeriod") or {}
                readings.append((_iso_to_epoch(rec.get("ts")), float(pct),
                                 per.get("start"), per.get("end")))
        if not readings:
            return 0, 0, -1
        readings.sort(key=lambda r: r[0] or 0.0)
        _, week_pct, cur_period, cur_end = readings[-1]
        lt = time.localtime()
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        # Baseline = last same-period reading before today; none ⇒ 0 (week began today).
        baseline = 0.0
        for ts, pct, ps, _pe in readings:
            if ps == cur_period and ts is not None and ts < midnight:
                baseline = pct
        today_pct = max(0.0, week_pct - baseline)
        clamp = lambda v: max(0, min(100, int(round(v))))
        return clamp(week_pct), clamp(today_pct), _mins_until(_iso_to_epoch(cur_end))
    except OSError:
        return 0, 0, -1
    except Exception as e:  # a log hiccup must never touch the Claude path
        log(f"Grok limit read failed: {e}")
        return 0, 0, -1


def _mins_until_midnight() -> int:
    """Minutes until the next local midnight — the 'today' bar's reset."""
    lt = time.localtime()
    next_midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 0, 0, 0, -1))
    return _mins_until(next_midnight)


async def fetch_grok_usage() -> dict:
    """Grok fields for the BLE payload, cached for GROK_RECOMPUTE_S:
      g/gd    = week/today $ activity (at API rates — a gauge, not a bill)
      gwp/gdp = week/today % of xAI's real weekly limit (today = consumed since midnight)
      gwr/gdr = minutes until the weekly limit resets / until local midnight
    Best-effort: any failure returns {} so the Claude display is never affected.
    No network, no PitCrew — this daemon is self-sufficient."""
    try:
        now = time.time()
        if now - _grok_cache["ts"] >= GROK_RECOMPUTE_S:
            week, today = _recompute_grok()
            wpct, dpct, wreset = read_grok_limit()
            _grok_cache.update(ts=now, week=week, today=today,
                               wpct=wpct, dpct=dpct, wreset=wreset)
        return {
            "g": round(_grok_cache["week"]),
            "gd": round(_grok_cache["today"]),
            "gwp": _grok_cache["wpct"],
            "gdp": _grok_cache["dpct"],
            "gwr": _grok_cache["wreset"],   # weekly-limit reset (mins)
            "gdr": _mins_until_midnight(),  # today's reset = local midnight (mins)
        }
    except Exception as e:
        log(f"Grok usage compute failed: {e}")
        return {}


def _build_file_logger() -> logging.Logger | None:
    """Create a rotating file logger for field diagnostics, or None.

    Autostart launches the tray under pythonw.exe, which has no console — stdout
    is discarded (and is in fact None, making print() unsafe). A rotating file is
    then the ONLY trail when the daemon stalls in the field. Windows-only: on the
    Linux dev box / CI the console print() suffices, and gating to win32 keeps the
    pure-helper unit tests from writing stray log files.
    """
    if sys.platform != "win32":
        return None
    logger = logging.getLogger("clawdmeter.daemon")
    if logger.handlers:
        return logger  # idempotent across re-import (tray imports this module)
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    path = base / "Clawdmeter" / "daemon.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=512 * 1024, backupCount=3, encoding="utf-8"
        )
    except OSError:
        return None  # best-effort — logging setup must never stop the daemon
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


_FILE_LOGGER = _build_file_logger()


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    # Under pythonw sys.stdout is None and print() would raise — guard it so a
    # missing console can never crash the daemon thread (the silent-freeze mode).
    try:
        print(line, flush=True)
    except (OSError, ValueError, AttributeError, RuntimeError):
        pass
    if _FILE_LOGGER is not None:
        _FILE_LOGGER.info(msg)


class AuthError(Exception):
    """Raised by poll_api on a genuine 401/403 — the token really is expired or
    invalid and the user must re-run `claude login`. Distinct from a None return,
    which means a TRANSIENT failure (network/DNS, timeout, rate-limit, 5xx) that
    must NOT be mislabeled as a token problem (SC#5: a boot-time `getaddrinfo
    failed` DNS blip wrongly fired the 'token expired' toast)."""

def read_chime_setting() -> str:
    """Read the `chime` option from the config file. One of: off|on.

    Defaults to "off" so the device stays silent until the user opts in.
    """
    try:
        if CONFIG_FILE.exists():
            for line in CONFIG_FILE.read_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                if key.strip().lower() == "chime":
                    val = val.strip().lower()
                    if val in ("off", "on"):
                        return val
    except OSError:
        pass
    return "off"


def read_clock_setting() -> str:
    """Read the `clock` option from the config file. One of: off|auto|12|24.

    Defaults to "off" so existing setups keep showing "Usage" until opted in.
    """
    try:
        if CONFIG_FILE.exists():
            for line in CONFIG_FILE.read_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                if key.strip().lower() == "clock":
                    val = val.strip().lower()
                    if val in ("off", "auto", "12", "24"):
                        return val
    except OSError:
        pass
    return "off"


def add_chime_field(payload: dict) -> None:
    """Add "c":1 to the payload when the config opts in, so the firmware may
    sound the session-reset chime. Omitted entirely when chime is off."""
    if read_chime_setting() == "on":
        payload["c"] = 1


def detect_hour_format() -> int:
    """Best-effort 12h/24h detection on Windows via the registry. Returns 12 or 24."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\International") as k:
            # iTime: "1" = 24-hour, "0" = 12-hour.
            val, _ = winreg.QueryValueEx(k, "iTime")
            return 24 if str(val).strip() == "1" else 12
    except (ImportError, OSError):
        return 24


def add_clock_fields(payload: dict) -> None:
    """Add "t" (local wall-clock epoch) + "tf" (12|24) when the config opts in."""
    clock = read_clock_setting()
    if clock == "off":
        return
    tf = 24 if clock == "24" else 12 if clock == "12" else detect_hour_format()
    payload["t"] = int(time.time()) + time.localtime().tm_gmtoff
    payload["tf"] = tf


async def poll_api(token: str) -> dict | None:
    headers = dict(API_HEADERS_TEMPLATE)
    headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as http:
            resp = await http.post(API_URL, headers=headers, json=API_BODY)
    except httpx.HTTPError as e:
        # Network/DNS/timeout — transient. Return None (no toast), retry next tick.
        log(f"API call failed: {e}")
        return None
    if resp.status_code in (401, 403):
        # Genuine auth rejection — the ONLY case that warrants the actionable
        # "run claude login" toast.
        log(f"API HTTP {resp.status_code}: {resp.text[:200]}")
        raise AuthError(resp.status_code)
    if resp.status_code >= 400:
        # Other 4xx/5xx (rate-limit, server error) — transient, not a token issue.
        log(f"API HTTP {resp.status_code}: {resp.text[:200]}")
        return None

    def hdr(name: str, default: str = "0") -> str:
        return resp.headers.get(name, default)

    now = time.time()

    def reset_minutes(reset_ts: str) -> int:
        try:
            r = float(reset_ts)
        except ValueError:
            return 0
        mins = (r - now) / 60.0
        return int(round(mins)) if mins > 0 else 0

    def pct(util: str) -> int:
        try:
            return int(round(float(util) * 100))
        except ValueError:
            return 0

    if resp.headers.get("anthropic-ratelimit-unified-5h-utilization"):
        payload = {
            "s": pct(hdr("anthropic-ratelimit-unified-5h-utilization")),
            "sr": reset_minutes(hdr("anthropic-ratelimit-unified-5h-reset")),
            "w": pct(hdr("anthropic-ratelimit-unified-7d-utilization")),
            "wr": reset_minutes(hdr("anthropic-ratelimit-unified-7d-reset")),
            "st": hdr("anthropic-ratelimit-unified-5h-status", "unknown"),
            "acct": "pro",
            "ok": True,
        }
    else:
        reset_ts = hdr("anthropic-ratelimit-unified-overage-reset")
        payload = {
            "s": pct(hdr("anthropic-ratelimit-unified-overage-utilization")),
            "sr": reset_minutes(reset_ts),
            "w": 0,
            "wr": 0,
            "st": hdr("anthropic-ratelimit-unified-status", "unknown"),
            "acct": "ent",
            **_billing_period_info(now, reset_ts),
            "ok": True,
        }
    add_chime_field(payload)   # adds "c":1 iff the config opts in
    add_clock_fields(payload)   # adds "t" + "tf" iff the config opts in
    payload.update(await fetch_grok_usage())  # adds "g"/"gd" iff PitCrew is up
    return payload


def _billing_period_info(now: float, reset_ts: str) -> dict:
    """Fraction of billing period elapsed (tp, 0-100) and period length in days (pd).

    Monthly window is assumed (headers expose only reset_ts, not period). Per the
    Claude Enterprise Admin API reference, spend-limit period's "only value today
    is monthly" — see the macOS daemon for the full note.
    """
    try:
        period_end = float(reset_ts)
    except ValueError:
        return {"tp": 0, "pd": 30, "rd": ""}
    if period_end <= 0:
        # reset_ts defaults to "0" whenever the overage-reset header is absent
        # (e.g. a 200 that simply carries no billing headers). fromtimestamp(0)
        # is 1970; stepping one month back lands in 1969, and datetime.timestamp()
        # raises OSError for pre-1970 dates on Windows — taking the whole poll
        # loop down. Bail out to the neutral default instead.
        return {"tp": 0, "pd": 30, "rd": ""}
    try:
        dt_end = datetime.datetime.fromtimestamp(period_end)
        prev_month = dt_end.month - 1 or 12
        prev_year = dt_end.year if dt_end.month > 1 else dt_end.year - 1
        prev_day = min(dt_end.day, calendar.monthrange(prev_year, prev_month)[1])
        dt_start = dt_end.replace(year=prev_year, month=prev_month, day=prev_day)
        period_start = dt_start.timestamp()
    except (OSError, OverflowError, ValueError):
        # Belt-and-braces beyond the <= 0 guard above (#104): Windows
        # datetime.timestamp()/fromtimestamp() also raise OSError(22)/
        # OverflowError/ValueError for out-of-range NON-zero values (e.g. a
        # far-future "99999999999999" header, which overflows fromtimestamp).
        # Garbage must never crash the daemon thread — degrade to the safe
        # default instead (field report: OSError(22) killed the poll loop).
        return {"tp": 0, "pd": 30, "rd": ""}
    period_len = period_end - period_start
    if period_len <= 0:
        return {"tp": 0, "pd": 30, "rd": ""}
    pct_val = (now - period_start) / period_len * 100
    return {
        "tp": max(0, min(100, int(round(pct_val)))),
        "pd": int(round(period_len / 86400)),
        "rd": f"{dt_end.strftime('%b')} {dt_end.day}",
    }


def _mac_from_pnp_instance_id(instance_id: str) -> str | None:
    """Recover a canonical BLE MAC ("AA:BB:CC:DD:EE:FF") from a PnP instance id.

    Windows encodes a paired BLE device's address in its PnP instance id as a
    12-hex run after a ``DEV_`` token, e.g.::

        BTHLE\\DEV_98A316A5D706\\7&B8081D1&0&98A316A5D706  ->  98:A3:16:A5:D7:06

    Returns None when no ``DEV_<12 hex>`` token is present. Pure — the
    subprocess that produces the instance id lives in discover_bonded_address().
    """
    m = re.search(r"DEV_([0-9A-Fa-f]{12})(?![0-9A-Fa-f])", instance_id)
    if not m:
        return None
    h = m.group(1).upper()
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


def discover_bonded_address() -> str | None:
    """Return the BLE address of the bonded Clawdmeter, or None.

    A device that is paired AND connected to Windows stops advertising, so
    BleakScanner can't see it (the steady state once paired — see
    README-windows.md). WinRT can still connect to it directly by address, so
    we recover that address from the OS:

    1. CLAWDMETER_BLE_ADDRESS env override (skips discovery — testing / pinning).
    2. Windows PnP table, filtered to the device's FriendlyName.

    Non-Windows or any failure returns None.
    """
    if override := os.environ.get("CLAWDMETER_BLE_ADDRESS"):
        return override.strip().upper()
    if sys.platform != "win32":
        return None
    command = (
        "Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.FriendlyName -eq '{DEVICE_NAME}' }} | "
        "Select-Object -ExpandProperty InstanceId"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as e:
        log(f"Bonded-address lookup failed: {e}")
        return None
    for line in result.stdout.splitlines():
        if mac := _mac_from_pnp_instance_id(line):
            return mac
    return None


async def acquire_target():
    """Return a connectable handle for the Clawdmeter, or None.

    Targets only the device bonded to THIS machine (via the PnP table /
    CLAWDMETER_BLE_ADDRESS) — it never scans for a nearby device by name, so it
    can't grab a stranger's or the wrong nearby unit. The device must be paired
    with Windows once first (the documented setup). Returns a BLEDevice or None.
    """
    # The Windows "Add device" pairing wizard can't connect to this unit (confirmed:
    # the device never logs a connection from it), but a direct WinRT connect works.
    # So we DON'T rely on the OS having paired it — we scan for the advertising device
    # and let the daemon bond it itself (client.pair() in connect_and_run).
    address = discover_bonded_address()  # env override, or PnP if it ever is OS-paired
    try:
        if address:
            dev = await BleakScanner.find_device_by_address(address, timeout=SCAN_TIMEOUT)
        else:
            dev = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=SCAN_TIMEOUT)
        if dev:
            log(f"Found {DEVICE_NAME} advertising at {dev.address}")
            return dev
    except Exception as e:
        log(f"Scan for {DEVICE_NAME} failed: {e}")
    # Fallback: OS-paired device that has stopped advertising — connect by address.
    if address:
        log(f"Not advertising; connecting to bonded address {address}")
        return BLEDevice(address, DEVICE_NAME, None)
    return None


class Session:
    def __init__(self, client: BleakClient) -> None:
        self.client = client
        self.refresh_requested = asyncio.Event()

    def _on_refresh(self, _char, _data: bytearray) -> None:
        log("Refresh requested by device")
        self.refresh_requested.set()

    async def setup_refresh_subscription(self) -> None:
        # The refresh subscription is optional — it only lets a button press trigger
        # an instant re-poll; the 60s loop works without it. On this Windows + bonded-
        # HID device it is also the ONE GATT op that hard-fails (WinRT cancels the CCCD
        # write / "Unreachable") and takes the whole link DOWN with it, producing a
        # connect→fail→disconnect→reconnect flap every ~10-20s (visible as the device
        # unpairing/repairing and the battery icon blinking). Plain writes succeed
        # through the same link, so skipping the subscribe keeps the connection stable
        # and just costs button-press instant-refresh. Opt back in with
        # CLAWDMETER_REFRESH_SUB=1 once the WinRT/bond interaction is understood.
        if os.environ.get("CLAWDMETER_REFRESH_SUB") != "1":
            return
        try:
            await self.client.start_notify(REQ_CHAR_UUID, self._on_refresh)
        except (BleakError, ValueError, OSError) as e:
            log(f"Refresh subscription unavailable: {e}")

    async def write_payload(self, payload: dict) -> bool:
        data = json.dumps(payload, separators=(",", ":")).encode()
        log(f"Sending: {data.decode()}")
        try:
            await self.client.write_gatt_char(RX_CHAR_UUID, data, response=False)
            return True
        except (BleakError, OSError) as e:
            # WinRT can raise a raw OSError/WinError (NOT wrapped as BleakError)
            # when the peer GATT server goes transiently unavailable mid-write —
            # the same failure class setup_refresh_subscription() guards against.
            # Returning False trips the zombie-link break -> clean reconnect,
            # rather than an uncaught exception killing the daemon thread (the
            # silent-freeze failure mode, SC#2 field report).
            log(f"Write failed: {e}")
            return False


def _extract_access_token(blob: str) -> str | None:
    """Pull the accessToken out of a credentials blob.

    Claude Code stores credentials as a JSON object; the blob may also be
    nested ({"claudeAiOauth": {"accessToken": "..."}}). Fall back to a
    regex match so unexpected shapes still work, and finally treat the
    blob as a raw token if nothing else matches.
    """
    blob = blob.strip()
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        # direct: {"accessToken": "..."}
        tok = data.get("accessToken")
        if isinstance(tok, str) and tok.strip():
            return tok
        # nested: {"claudeAiOauth": {"accessToken": "..."}}
        for v in data.values():
            if isinstance(v, dict):
                tok = v.get("accessToken")
                if isinstance(tok, str) and tok.strip():
                    return tok
    m = re.search(r'"accessToken"\s*:\s*"([^"]+)"', blob)
    if m:
        return m.group(1)
    # Raw token (no JSON wrapper) — must look plausible (sk-ant-... etc.)
    if re.fullmatch(r"[A-Za-z0-9_\-.~+/=]{20,}", blob):
        return blob
    return None


def _windows_credential_candidates() -> list[Path]:
    """Return the ordered list of credential file paths to probe (first hit wins).

    Priority:
    1. CLAUDE_CREDENTIALS_PATH env override (D-03, project-specific)
    2. CLAUDE_CONFIG_DIR env override (official Claude override)
    3. D-02 candidate list: home/.claude, LOCALAPPDATA/Claude, APPDATA/Claude
    """
    # Priority 1: project-specific env override (D-03)
    if override := os.environ.get("CLAUDE_CREDENTIALS_PATH"):
        return [Path(override)]
    # Priority 2: official CLAUDE_CONFIG_DIR env override
    if config_dir := os.environ.get("CLAUDE_CONFIG_DIR"):
        return [Path(config_dir) / ".credentials.json"]
    # Priority 3: D-02 candidate list — first hit wins
    home = Path.home()
    local_appdata = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    return [
        home / ".claude" / ".credentials.json",          # primary (confirmed by docs)
        local_appdata / "Claude" / ".credentials.json",  # fallback 2
        appdata / "Claude" / ".credentials.json",        # fallback 3
    ]


def read_token() -> str | None:
    """Read the Claude OAuth access token from the first available credential file."""
    for path in _windows_credential_candidates():
        try:
            return _extract_access_token(path.read_text(encoding="utf-8"))
        except OSError:
            continue
    return None


def _read_expiry() -> str:
    """Return human-readable expiry from the first-hit credentials file.

    Reads claudeAiOauth.expiresAt (epoch milliseconds — JS convention).
    Divides by 1000 before passing to fromtimestamp (Python expects seconds).
    Returns 'expiry unknown' on any parse failure.
    """
    for path in _windows_credential_candidates():
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            data = json.loads(raw)
            oauth = data.get("claudeAiOauth", {})
            expires_ms = oauth.get("expiresAt")
            if expires_ms is None:
                return "expiry unknown"
            # CRITICAL: expiresAt is JS-convention epoch milliseconds; divide by 1000
            # before fromtimestamp (Python expects seconds). Raw value -> year ~57000.
            dt = datetime.datetime.fromtimestamp(
                expires_ms / 1000, tz=datetime.timezone.utc
            )
            return dt.strftime("%Y-%m-%d %H:%M UTC")
        except (TypeError, ValueError, OSError, AttributeError, json.JSONDecodeError):
            return "expiry unknown"
    return "expiry unknown"


async def _wait_first(*events: asyncio.Event, timeout: float) -> None:
    """Return when any of `events` is set, or after `timeout` seconds.

    Lets the poll loop's TICK wait wake immediately on a stop signal (clean,
    responsive Quit) without losing the refresh-request wakeup — instead of
    waiting only on refresh_requested and re-checking stop_event up to TICK
    later. Cancels and drains the loser tasks so they don't warn.
    """
    tasks = [asyncio.ensure_future(e.wait()) for e in events]
    try:
        await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def connect_and_run(device, stop_event: asyncio.Event, tray_state=None) -> bool:
    """Connect to device and poll until disconnected or stopped.

    Returns True if at least one successful write occurred.

    `device` is a BLEDevice — either from an advertisement scan or built from the
    bonded address by acquire_target(). The getattr keeps the log line robust if a
    bare address string is ever passed in.
    """
    log(f"Connecting to {getattr(device, 'address', device)}...")
    # D-01: retry wrapper — defeats WinRT post-wake failure modes
    # (Could not get GATT services: Unreachable, stale is_connected).
    # Rebuild a fresh BleakClient each attempt (locked D-05 recipe).
    client = None
    for attempt in range(CONNECT_RETRIES):
        # WinRT-backend options MUST be nested under winrt={...} — passing them as
        # top-level kwargs silently drops them. use_cached_services=False forces a
        # fresh GATT read (DIY firmware; cache may be stale after a reflash).
        client = BleakClient(
            device,
            winrt={"use_cached_services": False},
        )
        try:
            await client.connect()
            # The device requires a bonded+encrypted link for writes, but the Windows
            # pairing wizard can't reach it. Bond it ourselves over the live connection.
            # Idempotent: a no-op / benign error if already paired, so we swallow it.
            try:
                await client.pair()
                log("Paired (programmatic)")
            except Exception as pe:
                log(f"pair() note: {type(pe).__name__}: {pe}")
        except (BleakError, OSError, asyncio.TimeoutError, AssertionError) as e:
            # WinRT service discovery inside connect() can surface a raw OSError
            # (WinError) or even a bare AssertionError from bleak's FutureLike
            # (assert self._result) when the peer drops the link mid-discovery —
            # neither is wrapped as BleakError. Treat them as a normal failed
            # attempt so the D-01 retry loop handles them, instead of letting an
            # uncaught exception kill the daemon thread (the "daemon crashed"
            # tray toast + silent polling stop, field report).
            log(f"Connection attempt {attempt + 1}/{CONNECT_RETRIES} failed: {type(e).__name__}: {e}")
            try:
                await client.disconnect()
            except BleakError:
                pass
            if attempt < CONNECT_RETRIES - 1:
                await asyncio.sleep(CONNECT_RETRY_DELAY)
            continue

        if not client.is_connected:
            log(f"Connection attempt {attempt + 1}/{CONNECT_RETRIES} failed (not connected)")
            try:
                await client.disconnect()
            except BleakError:
                pass
            if attempt < CONNECT_RETRIES - 1:
                await asyncio.sleep(CONNECT_RETRY_DELAY)
            continue

        # Connected successfully
        break
    else:
        log(f"Connection failed after {CONNECT_RETRIES} attempts")
        return False

    log("Connected")
    session = Session(client)
    await session.setup_refresh_subscription()

    last_poll = 0.0  # D-03: poll immediately on first connect
    used_successfully = False
    consecutive_failures = 0  # D-03: zombie-link break counter
    try:
        while client.is_connected and not stop_event.is_set():
            now = time.time()
            elapsed = now - last_poll
            if session.refresh_requested.is_set() or elapsed >= POLL_INTERVAL:
                session.refresh_requested.clear()
                token = read_token()  # D-09: fresh each cycle
                if not token:
                    log("No token; skipping poll")
                    if tray_state:
                        tray_state.set_error("token expired — run claude login")
                else:
                    try:
                        payload = await poll_api(token)
                    except AuthError:
                        # Real 401/403 — token genuinely needs a refresh.
                        if tray_state:
                            tray_state.set_error("token expired — run claude login")
                        payload = None
                    if payload is not None:
                        if await session.write_payload(payload):
                            last_poll = time.time()
                            used_successfully = True
                            consecutive_failures = 0  # D-03: reset on success
                            if tray_state:
                                tray_state.set_connected(time.time())
                        else:
                            consecutive_failures += 1
                            if consecutive_failures >= ZOMBIE_BREAK_LIMIT:
                                log(
                                    f"Zombie link detected ({consecutive_failures} consecutive"
                                    f" write failures); abandoning connection"
                                )
                                break
                    # else: payload is None from a TRANSIENT failure (network/DNS,
                    # timeout, rate-limit, 5xx). poll_api already logged it; do NOT
                    # toast "token expired" — that mislabeled a boot-time DNS blip
                    # as an auth problem (SC#5). Leave tray state unchanged; the next
                    # tick retries and set_connected() recovers it.

            # Wake on a refresh request OR a stop, whichever comes first. Waking
            # promptly on stop_event is what lets the finally below run
            # client.disconnect() before the process exits, so the peer gets a
            # clean GATT disconnect (returns to its waiting screen) instead of
            # being left frozen on stale data after Quit (SC#3 graceful shutdown).
            await _wait_first(session.refresh_requested, stop_event, timeout=TICK)
    finally:
        # Clean GATT disconnect on the way out — this is what tells the peripheral
        # the link is gone. WinRT can surface a raw OSError (not BleakError) here,
        # so swallow both; the link tears down regardless once we exit.
        try:
            await client.disconnect()
        except (BleakError, OSError, AssertionError):
            # bleak's WinRT disconnect() also has bare asserts (e.g. assert char
            # while tearing down notifications on an already-gone peer); swallow
            # it too — the link tears down regardless once we exit.
            pass

    log("Device disconnected" if not stop_event.is_set() else "Stopping")
    return used_successfully


def _next_backoff(current: int, cap: int) -> int:
    """D-05: double current backoff value, clamped to cap.

    Pure helper — unit-testable without driving the main loop.
    Used by both slow-search (cap=60) and fast-reconnect (cap=RECONNECT_BACKOFF_CAP) regimes.
    """
    return min(current * 2, cap)


async def main(tray_state=None) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    # Populate the shared state object so the tray can route Quit through
    # loop.call_soon_threadsafe (RESEARCH Pitfall 2).  Additive — the existing
    # stop_event = asyncio.Event() line above is unchanged.
    if tray_state is not None:
        tray_state.loop = loop
        tray_state.stop_event = stop_event

    def _stop(*_args: object) -> None:
        log("Daemon stopping")
        stop_event.set()

    # OS signal handlers can only be installed from the main thread, and
    # loop.add_signal_handler is unsupported on Windows. When running under the
    # tray (04-03) the loop lives in a background thread and the tray owns clean
    # shutdown via stop_event (loop.call_soon_threadsafe), so skip silently there.
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _stop)
            except NotImplementedError:
                # Windows: add_signal_handler not supported; fall back to signal.signal
                try:
                    signal.signal(sig, _stop)
                except ValueError:
                    # Not the main thread of the main interpreter — tray owns shutdown.
                    pass

    log("=== Claude Usage Tracker Daemon (BLE, Windows) ===")
    log(f"Poll interval: {POLL_INTERVAL}s")

    # D-05: two distinct backoff regimes — slow-search (device absent) vs fast-reconnect (link dropped)
    search_backoff = 1     # caps at 60s — gentle, for a device that is genuinely absent/off
    reconnect_backoff = 1  # caps at RECONNECT_BACKOFF_CAP — fast, to clear the 120s SLA after a drop
    while not stop_event.is_set():
        device = await acquire_target()
        if not device:
            # Slow-search regime: device was not found by scan — back off gently
            if tray_state:
                tray_state.set_scanning()
            log(f"Device not found, retrying in {search_backoff}s...")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=search_backoff)
            except asyncio.TimeoutError:
                pass
            search_backoff = _next_backoff(search_backoff, 60)
            continue

        ok = await connect_and_run(device, stop_event, tray_state)
        if not ok:
            # Fast-reconnect regime: had/attempted a link that dropped — retry quickly
            if tray_state:
                tray_state.set_scanning()
            log(f"Connection lost, reconnecting in {reconnect_backoff}s...")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=reconnect_backoff)
            except asyncio.TimeoutError:
                pass
            reconnect_backoff = _next_backoff(reconnect_backoff, RECONNECT_BACKOFF_CAP)
        else:
            # Successful session — reset reconnect counter to floor; search_backoff also reset
            reconnect_backoff = 1
            search_backoff = 1


if __name__ == "__main__":
    if sys.platform != "win32":
        print(
            "Warning: running under Linux/WSL — WinRT BLE will not be available.",
            file=sys.stderr,
        )
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
