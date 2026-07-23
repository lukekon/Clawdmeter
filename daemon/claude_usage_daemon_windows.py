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
import sys
import threading
import time
from pathlib import Path

import httpx
import serial
import serial.tools.list_ports

DEVICE_NAME = "Clawdmeter"

# --- USB-serial transport --------------------------------------------------
# The device is a USB-powered desk gauge that is physically cabled to this PC at
# all times (it has no battery — unplugging powers it off). Windows' BLE stack
# proved unable to hold a connection to it, so the daemon streams the same JSON
# usage payload over the USB-serial link (the ESP32-S3 native USB-CDC, the same
# port used for flashing/screenshots) instead of the BLE RX characteristic. The
# firmware accepts a newline-terminated JSON line on that port as a data write.
SERIAL_BAUD = 115200
ESP32_VID = 0x303A         # Espressif — the ESP32-S3 native USB-JTAG/CDC vendor id
SERIAL_WRITE_TIMEOUT = 5.0

POLL_INTERVAL = 60
PORT_RETRY_BACKOFF_CAP = 30  # gentle backoff while the port is absent (device unplugged / reflashing)

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


def find_serial_port() -> str | None:
    """Return the COM port of the cabled Clawdmeter, or None.

    Priority:
      1. CLAWDMETER_SERIAL_PORT env override (pinning / testing).
      2. The Espressif native USB-CDC port (VID 0x303A) — the ESP32-S3.
      3. If exactly one serial port is present, use it.

    Returns None when nothing plausible is present — device unplugged, or the
    port is momentarily held by a flash/screenshot — so the caller backs off and
    retries rather than crashing.
    """
    if override := os.environ.get("CLAWDMETER_SERIAL_PORT"):
        return override.strip()
    ports = list(serial.tools.list_ports.comports())
    for p in ports:
        if p.vid == ESP32_VID:
            return p.device
    if len(ports) == 1:
        return ports[0].device
    return None


def open_serial(port: str) -> "serial.Serial":
    """Open the port WITHOUT asserting DTR/RTS.

    On the ESP32-S3 native USB-CDC (USB-Serial-JTAG), asserting DTR/RTS on open
    is the auto-reset line esptool uses — a plain open would reboot the device to
    its splash screen every time the daemon (re)connects. Pre-setting both lines
    low before open keeps the device running so it just picks up the next payload.
    """
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = SERIAL_BAUD
    ser.timeout = 1
    ser.write_timeout = SERIAL_WRITE_TIMEOUT
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


def write_payload(ser: "serial.Serial", payload: dict) -> None:
    """Write one usage payload as a newline-terminated JSON line.

    The firmware treats a line beginning with '{' on the USB-serial port as a
    data write (transport-equivalent to the old BLE RX characteristic). Raises
    serial.SerialException / OSError if the port has gone away (device unplugged
    or reflashing), which the main loop catches to reopen.
    """
    line = json.dumps(payload, separators=(",", ":")) + "\n"
    log(f"Sending: {line.strip()}")
    ser.write(line.encode())
    ser.flush()


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


async def poll_and_send(ser: "serial.Serial", tray_state=None) -> None:
    """Poll the API once and stream the payload over the serial link.

    Reads a fresh token each call. Token/API failures are handled inline (they
    don't invalidate the cable); a serial write error propagates to the caller,
    which reopens the port. Silent no-op when there's nothing valid to send.
    """
    token = read_token()  # fresh each cycle
    if not token:
        log("No token; skipping poll")
        if tray_state:
            tray_state.set_error("token expired — run claude login")
        return
    try:
        payload = await poll_api(token)
    except AuthError:
        # Real 401/403 — token genuinely needs a refresh.
        if tray_state:
            tray_state.set_error("token expired — run claude login")
        return
    if payload is None:
        # Transient failure (network/DNS, timeout, rate-limit, 5xx). poll_api
        # already logged it; do NOT toast "token expired" (that mislabeled a
        # boot-time DNS blip as an auth problem, SC#5). Next poll retries.
        return
    write_payload(ser, payload)   # raises serial.SerialException/OSError on a dead port
    if tray_state:
        tray_state.set_connected(time.time())


def _next_backoff(current: int, cap: int) -> int:
    """Double `current`, clamped to `cap`. Pure helper for the port-retry backoff."""
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

    log("=== Claude Usage Tracker Daemon (USB serial, Windows) ===")
    log(f"Poll interval: {POLL_INTERVAL}s")

    async def sleep_or_stop(secs: float) -> None:
        """Sleep up to `secs`, waking immediately if a stop is requested."""
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=secs)
        except asyncio.TimeoutError:
            pass

    ser = None
    port_backoff = 1
    try:
        while not stop_event.is_set():
            # (Re)open the serial port if we don't have a live one. The port is
            # briefly absent when the device is unplugged or while it's being
            # reflashed, so we back off gently rather than spin.
            if ser is None:
                port = find_serial_port()
                if not port:
                    if tray_state:
                        tray_state.set_scanning()
                    log(f"Clawdmeter serial port not found; retrying in {port_backoff}s...")
                    await sleep_or_stop(port_backoff)
                    port_backoff = _next_backoff(port_backoff, PORT_RETRY_BACKOFF_CAP)
                    continue
                try:
                    ser = open_serial(port)
                except (serial.SerialException, OSError) as e:
                    # Port exists but is held (a flash/screenshot has it) or vanished
                    # between listing and opening — treat like "not found".
                    log(f"Opening {port} failed: {e}")
                    ser = None
                    if tray_state:
                        tray_state.set_scanning()
                    await sleep_or_stop(port_backoff)
                    port_backoff = _next_backoff(port_backoff, PORT_RETRY_BACKOFF_CAP)
                    continue
                log(f"Opened {port} @ {SERIAL_BAUD} baud")
                port_backoff = 1

            # One poll + send. A serial error means the cable/device went away —
            # drop the port and reopen on the next iteration.
            try:
                await poll_and_send(ser, tray_state)
            except (serial.SerialException, OSError) as e:
                log(f"Serial link lost: {e}")
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
                if tray_state:
                    tray_state.set_scanning()
                await sleep_or_stop(1)
                continue

            await sleep_or_stop(POLL_INTERVAL)
    finally:
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
    log("Stopping")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
