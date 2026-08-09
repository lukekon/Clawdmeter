#!/usr/bin/env python3
"""Claude Usage Tracker Daemon — Windows (Phase 2).

Reads the Claude OAuth token from the native-Windows credentials path and
polls the Anthropic API for rate-limit utilization data. BLE glue added in
later plans.
"""

import asyncio
import base64
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

try:  # package import when run as -m daemon..., plain when run as a script
    from daemon import market_logos
except ImportError:  # pragma: no cover
    import market_logos
import serial
import serial.tools.list_ports

DEVICE_NAME = "Clawdmeter"

# Suppress the console window a child powershell.exe would otherwise flash on
# screen. The daemon runs windowless (wscript/pythonw), so any console-subsystem
# child with no explicit flag allocates its OWN window — that was the ~10s
# PowerShell flash on every vitals refresh. 0 (no-op) off Windows.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

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

# Mouse-button view control: a global low-level hook maps the two thumb side
# buttons to the device's view cycle (XButton1/Back -> prev, XButton2/Fwd ->
# next), sending the same "pprev"/"pnext" serial commands the physical side
# buttons trigger. The hook runs in its own thread (Win32 message pump) and only
# ENQUEUES commands; the asyncio loop owns the serial port and flushes them, so
# there is never a cross-thread write. The buttons are consumed (dedicated to the
# device — they stop acting as browser Back/Forward), per the chosen config.
import collections
_view_cmds: "collections.deque[str]" = collections.deque()
_hook_refs: list = []   # keep the ctypes callback alive (else it's GC'd mid-hook)

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
_grok_cache = {"ts": 0.0, "week": 0.0, "today": 0.0, "wpct": 0, "dpct": 0,
               "wreset": -1, "daily": [0.0] * 7}
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


def _recompute_grok() -> tuple[float, float, list]:
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
    daily = [0.0] * 7  # index 6 = today, 0 = 6 days ago (the 7-day sparkline)

    def add(cost: float, ts: float) -> None:
        nonlocal week, today
        if cost <= 0:
            return
        if ts >= week_cut:
            week += cost
        if ts >= today_cut:
            today += cost
        idx = 6 + int((ts - today_cut) // 86400)
        if 0 <= idx < 7:
            daily[idx] += cost

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

    return week, today, daily


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
            week, today, daily = _recompute_grok()
            wpct, dpct, wreset = read_grok_limit()
            _grok_cache.update(ts=now, week=week, today=today, daily=daily,
                               wpct=wpct, dpct=dpct, wreset=wreset)
        return {
            "g": round(_grok_cache["week"]),
            "gd": round(_grok_cache["today"]),
            "gwp": _grok_cache["wpct"],
            "gdp": _grok_cache["dpct"],
            "gwr": _grok_cache["wreset"],   # weekly-limit reset (mins)
            "gdr": _mins_until_midnight(),  # today's reset = local midnight (mins)
            # 7-day $ activity series for the GROK view sparkline (index 6 = today).
            "gx": {"wk": [round(x, 4) for x in _grok_cache["daily"]]},
        }
    except Exception as e:
        log(f"Grok usage compute failed: {e}")
        return {}


# --- Kimi (Moonshot) activity + real 5h/7d limits ---------------------------
# Two planes, like Grok: $ activity is computed HERE from the local Kimi Code
# wire logs (no network), while the 5h/7d limit % come from Moonshot's own usage
# endpoint (the numbers the kimi.com "My Quota" panel shows). The endpoint is the
# one the Kimi Code extension itself calls; the OAuth token lives in the CLI's
# credential file, kept fresh by the extension while it runs. When the token is
# stale (extension closed a while), the limits hold their last-good reading and
# only the $ activity keeps updating — the view never shows an invented number.
KIMI_HOME = Path(os.environ.get("KIMI_CODE_HOME", Path.home() / ".kimi-code"))
KIMI_SESSIONS = KIMI_HOME / "sessions"
KIMI_CRED = KIMI_HOME / "credentials" / "kimi-code.json"
KIMI_USAGE_URL = os.environ.get(
    "KIMI_CODE_BASE_URL", "https://api.kimi.com/coding/v1").rstrip("/") + "/usages"
# $ per 1M tokens (input, output, cacheRead, cacheCreation). Moonshot k-series;
# keep in sync with PitCrew's lib/usage-rates.ts. UNLIKE Grok, the four token
# buckets in usage.record are DISJOINT (inputOther + cacheRead + cacheCreation sum
# to the prompt), so each is billed at its own rate with no subtraction.
KIMI_RATES = (3.0, 15.0, 0.30, 3.0)
KIMI_RECOMPUTE_S = 300
# Reset times are stored as ABSOLUTE epochs (not pre-computed minutes): the token
# is short-lived (15-min TTL, refreshed only while the Kimi extension runs), so
# between live reads we recompute minutes-until fresh on every poll from the epoch.
# Storing minutes would freeze the countdown at its last-read value while the real
# (wall-clock) reset marched past.
#
# We CAN'T refresh the token ourselves (that's an auth-flow guardrail), so when the
# extension is closed the /usages read 401s. To keep the Kimi view useful anyway we
# (1) persist the last-good limits to a sidecar so they survive daemon restarts, and
# (2) PROJECT them forward: the countdown ticks off the stored epoch, and once a
# window's reset epoch passes we know it rolled over — usage is back to ~0 (you're
# idle if the extension's closed) — so we zero the % and advance to the next
# boundary. A later live read corrects everything.
_kimi_cache = {"ts": 0.0, "week": 0.0, "today": 0.0, "daily": [0.0] * 7, "model": "",
               "s_pct": 0, "s_reset_epoch": None, "s_window_min": 300,
               "w_pct": 0, "w_reset_epoch": None, "w_window_min": 10080,
               "lim_valid": False}
_KIMI_SIDECAR = (Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
                 / "Clawdmeter" / "kimi_limits.json")
_KIMI_SIDECAR_KEYS = ("s_pct", "s_reset_epoch", "s_window_min",
                      "w_pct", "w_reset_epoch", "w_window_min")


def _save_kimi_sidecar() -> None:
    """Persist last-good limits so a daemon restart doesn't blank the Kimi view."""
    try:
        _KIMI_SIDECAR.parent.mkdir(parents=True, exist_ok=True)
        _KIMI_SIDECAR.write_text(
            json.dumps({k: _kimi_cache[k] for k in _KIMI_SIDECAR_KEYS}), encoding="utf-8")
    except OSError:
        pass  # best-effort — persistence must never disturb the poll loop


def _load_kimi_sidecar() -> None:
    """Seed the cache from the sidecar on startup (best-effort)."""
    try:
        saved = json.loads(_KIMI_SIDECAR.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for k in _KIMI_SIDECAR_KEYS:
        if k in saved and saved[k] is not None:
            _kimi_cache[k] = saved[k]


def _project_window(pct: int, epoch, window_min, now: float) -> tuple[int, int]:
    """Last-good (pct, reset_epoch) → (pct_now, reset_mins_now), aged off wall-clock.
    Before the reset: the stored % still holds, countdown ticks down. After it: the
    window rolled, so % is 0 and we advance the epoch by whole windows to the next
    future boundary (handles several missed windows if the daemon was off a while)."""
    if not epoch:
        return pct, -1
    if now < epoch:
        return pct, max(0, int((epoch - now) // 60))
    if window_min and window_min > 0:
        span = window_min * 60.0
        k = int((now - epoch) // span) + 1
        epoch = epoch + k * span
        return 0, max(0, int((epoch - now) // 60))
    return 0, -1


def _kimi_label(model: str) -> str:
    """'kimi-code/k3' → 'K3'. Compact chip label; generic so a new k-model needs
    no code change (the trailing context suffix like '-256k' is dropped)."""
    seg = (model or "").split("/")[-1]
    m = re.match(r"k(\d+)", seg)
    if m:
        return "K" + m.group(1)
    if "highspeed" in seg:
        return "K2.7 HS"
    if "coding" in seg:
        return "K2.7"
    return seg.upper()[:10] if seg else ""


def _recompute_kimi() -> tuple[float, float, list, str]:
    """Scan the local Kimi Code wire logs for this week's / today's $ activity and
    the model in use now. One wire.jsonl per agent under sessions/<wd>/<sid>/
    agents/<agent>/; only `usage.record` with usageScope=='turn' carries per-call
    tokens (mirrors PitCrew's ingestKimiSessions). Files older than the week are
    skipped whole. Returns (week$, today$, daily[7], in_use_label)."""
    now = time.time()
    week_cut = now - 7 * 86400
    lt = time.localtime(now)
    today_cut = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    week = today = 0.0
    daily = [0.0] * 7  # index 6 = today
    ci, co, cr, cc = KIMI_RATES
    last_ts, last_model = 0.0, ""

    try:
        wires = list(KIMI_SESSIONS.glob("*/*/agents/*/wire.jsonl"))
    except OSError:
        wires = []
    for f in wires:
        try:
            if f.stat().st_mtime < week_cut:
                continue
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"usage.record"' not in line:  # cheap prefilter
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("type") != "usage.record" or rec.get("usageScope") != "turn":
                        continue
                    u = rec.get("usage") or {}
                    ts_ms = rec.get("time")
                    if not ts_ms:
                        continue
                    ts = ts_ms / 1000.0
                    cost = (
                        (u.get("inputOther") or 0) * ci
                        + (u.get("output") or 0) * co
                        + (u.get("inputCacheRead") or 0) * cr
                        + (u.get("inputCacheCreation") or 0) * cc
                    ) / 1_000_000.0
                    if ts > last_ts:
                        last_ts, last_model = ts, rec.get("model") or ""
                    if cost <= 0:
                        continue
                    if ts >= week_cut:
                        week += cost
                    if ts >= today_cut:
                        today += cost
                    idx = 6 + int((ts - today_cut) // 86400)
                    if 0 <= idx < 7:
                        daily[idx] += cost
        except OSError:
            continue

    # "In use now" = the model of the most recent record, but only if it's recent
    # (a parallel session running now), else idle — same spirit as the Claude chip.
    model = _kimi_label(last_model) if (now - last_ts) <= 900 else ""
    return week, today, daily, model


def _window_minutes(w: dict) -> float:
    """A limit window's duration in minutes (proto-style timeUnit enum)."""
    d = w.get("duration") or 0
    unit = w.get("timeUnit") or ""
    if "HOUR" in unit:
        return d * 60
    if "DAY" in unit:
        return d * 1440
    if "SECOND" in unit:
        return d / 60.0
    return d  # TIME_UNIT_MINUTE (or unspecified)


def _pct_of(detail: dict) -> int:
    """used/limit → 0-100 int, clamped; 0 when limit is missing/zero."""
    try:
        limit = float(detail.get("limit") or 0)
        used = float(detail.get("used") or 0)
    except (TypeError, ValueError):
        return 0
    if limit <= 0:
        return 0
    return max(0, min(100, int(round(used / limit * 100))))


async def _read_kimi_limits() -> dict | None:
    """Moonshot's real 5h + 7d Kimi Code limits from GET /coding/v1/usages, or None
    on any failure (missing/expired token, network, unexpected shape). Response:
    top-level `usage` = the 7-day window; `limits[]` carries the shorter windows
    (the 300-minute = 5-hour one is the session). Numbers arrive as decimal strings."""
    try:
        cred = json.loads(KIMI_CRED.read_text(encoding="utf-8"))
        token = cred.get("access_token")
        if not token:
            return None
    except (OSError, ValueError):
        return None
    try:
        async with httpx.AsyncClient(timeout=20.0) as http:
            resp = await http.get(KIMI_USAGE_URL,
                                  headers={"Authorization": f"Bearer {token}",
                                           "Accept": "application/json"})
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        log(f"Kimi limit read failed: {e}")
        return None
    except Exception as e:  # a usage-endpoint hiccup must never touch the Claude path
        log(f"Kimi limit read error: {e}")
        return None
    weekly = data.get("usage") or {}
    limits = data.get("limits") or []
    # Session = the shortest-window limit (the 5-hour / 300-min one).
    session = None
    for l in limits:
        if _window_minutes(l.get("window") or {}) <= 0:
            continue
        if session is None or _window_minutes(l["window"]) < _window_minutes(session["window"]):
            session = l
    s_detail = (session or {}).get("detail") or {}
    return {
        "s_pct": _pct_of(s_detail),
        "s_reset_epoch": _iso_to_epoch(s_detail.get("resetTime")),
        "s_window_min": _window_minutes((session or {}).get("window") or {}) or 300,
        "w_pct": _pct_of(weekly),
        "w_reset_epoch": _iso_to_epoch(weekly.get("resetTime")),
        "w_window_min": _window_minutes(weekly.get("window") or {}) or 10080,
    }


async def fetch_kimi_usage() -> dict:
    """Kimi fields for the payload, cached for KIMI_RECOMPUTE_S:
      km/kmd     = week/today $ activity (API rates — a gauge, not a bill)
      kms/kmsr   = 5-hour window % + reset (mins)
      kmw/kmwr   = 7-day window % + reset (mins)
      kml        = 1 when the % above are live (token was fresh this poll), else 0
      kmx        = 7-day $ activity series for the sparkline (index 6 = today)
      kmm        = model in use now (e.g. "K3"), "" when idle
    Returns {} (device hides the Kimi view) only when there is no signal at all."""
    try:
        now = time.time()
        if _kimi_cache["ts"] == 0.0:
            _load_kimi_sidecar()   # first call — restore last-good across restarts
        if now - _kimi_cache["ts"] >= KIMI_RECOMPUTE_S:
            week, today, daily, model = _recompute_kimi()
            _kimi_cache.update(ts=now, week=week, today=today, daily=daily, model=model)
            lim = await _read_kimi_limits()
            if lim:
                _kimi_cache.update(lim_valid=True, **lim)  # refresh last-good
                _save_kimi_sidecar()                       # persist for next restart
            else:
                _kimi_cache["lim_valid"] = False           # keep/project last-good
        c = _kimi_cache
        # Age the last-good limits off wall-clock: countdown ticks down, and a rolled
        # window (reset epoch passed while idle) zeroes its % and rolls to the next.
        s_pct, s_reset = _project_window(c["s_pct"], c["s_reset_epoch"], c["s_window_min"], now)
        w_pct, w_reset = _project_window(c["w_pct"], c["w_reset_epoch"], c["w_window_min"], now)
        if (c["week"] == 0 and c["today"] == 0
                and not s_pct and not w_pct and not c["lim_valid"]
                and c["s_reset_epoch"] is None and c["w_reset_epoch"] is None):
            return {}   # Kimi never used and no limit ever read → no view
        return {
            "km": round(c["week"]),
            "kmd": round(c["today"]),
            "kms": s_pct,
            "kmsr": s_reset,
            "kmw": w_pct,
            "kmwr": w_reset,
            "kml": 1 if c["lim_valid"] else 0,
            "kmx": {"wk": [round(x, 4) for x in c["daily"]]},
            "kmm": c["model"],
        }
    except Exception as e:
        log(f"Kimi usage compute failed: {e}")
        return {}


# --- Codex (OpenAI) activity + weekly limit ---------------------------------
# One plane, fully local (unlike Kimi there is no token to go stale): both the
# $ activity and the weekly-limit % come from Codex's own rollout logs under
# ~/.codex/sessions/<yyyy>/<mm>/<dd>/rollout-*.jsonl — one JSONL per session.
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
CODEX_SESSIONS = CODEX_HOME / "sessions"
# $ per 1M tokens (input, output, cacheRead). gpt-5.6 family, verified
# 2026-08-06; keep in sync with PitCrew's lib/usage-rates.ts. Longest prefix
# first, like XAI_RATES.
CODEX_RATES = [
    ("gpt-5.6-sol", (5.0, 30.0, 0.50)),
    ("gpt-5.6-terra", (2.0, 12.0, 0.20)),
    ("gpt-5.6-luna", (0.20, 1.20, 0.02)),
]
CODEX_RECOMPUTE_S = 300
# Reset stored as an ABSOLUTE epoch (b24e57a): minutes-until is recomputed fresh
# on every poll so the countdown never freezes between log writes.
_codex_cache = {"ts": 0.0, "week": 0.0, "today": 0.0, "daily": [0.0] * 7, "model": "",
                "w_pct": 0, "w_reset_epoch": None, "w_window_min": 10080}


def _codex_label(model: str) -> str:
    """'gpt-5.6-sol' → '5.6 SOL'. Compact chip label; generic so a new gpt model
    needs no code change (the 'gpt-' prefix is dropped — the view already says
    Codex)."""
    seg = (model or "").split("/")[-1]
    if seg.lower().startswith("gpt-"):
        seg = seg[4:]
    return seg.upper().replace("-", " ")[:10] if seg else ""


def _codex_cost(model: str, fresh: int, output: int, cached: int) -> float:
    """$ at API rates for one Codex turn, or 0 for an unknown model. `fresh` is
    already the non-cached input (see the subset note in _recompute_codex)."""
    for prefix, (ci, co, cc) in CODEX_RATES:
        if model.startswith(prefix):
            return (fresh * ci + output * co + cached * cc) / 1_000_000.0
    return 0.0


def _recompute_codex() -> tuple[float, float, list, str, dict]:
    """Scan the local Codex rollout logs for this week's / today's $ activity,
    the model in use now, and the live weekly limit.

    Usage lives on `event_msg` records whose payload.type is `token_count`; use
    payload.info.last_token_usage (the per-API-call delta) — NEVER sum
    total_token_usage, a cumulative snapshot that resets on compaction. Token
    buckets carry the same subset trap as Grok: cached_input_tokens is a SUBSET
    of input_tokens, so fresh input = input - cached (billing it as-is would
    massively overstate cost); reasoning_output_tokens is already inside
    output_tokens (do not add); cache_write_input_tokens is 0 in practice.

    token_count carries NO model — the model lives on `turn_context` records.
    Forked sub-agent rollouts bury their token_counts BEFORE the only
    turn_context, so per-position attribution is impossible: a file's events are
    attributed to the file's FIRST turn_context model, or (files with none —
    e.g. Codex Desktop sessions) to the newest turn_context model seen anywhere.

    The weekly limit rides the same records: payload.rate_limits = {primary:
    {used_percent, window_minutes, resets_at}, secondary: null, ...}. We take the
    NEWEST token_count with a non-null rate_limits (sub-agent files carry
    rate_limits: null — skip those). Codex exposes only the 7-day window
    (secondary is null), so there is no session/daily ring. Files older than the
    week are skipped whole. Returns (week$, today$, daily[7], in_use_label,
    limits)."""
    now = time.time()
    week_cut = now - 7 * 86400
    lt = time.localtime(now)
    today_cut = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    week = today = 0.0
    daily = [0.0] * 7  # index 6 = today
    files = []          # (file_model, [(ts, fresh, output, cached), ...])
    newest_ctx_ts, newest_ctx_model = 0.0, ""
    lim_ts, limits = 0.0, {}

    try:
        rollouts = list(CODEX_SESSIONS.glob("*/*/*/rollout-*.jsonl"))
    except OSError:
        rollouts = []
    for f in rollouts:
        try:
            if f.stat().st_mtime < week_cut:
                continue
            file_model, events = "", []
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"token_count"' not in line and '"turn_context"' not in line:
                        continue  # cheap prefilter over a big log
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    ts = _iso_to_epoch(rec.get("timestamp"))
                    if not ts:
                        continue
                    if rec.get("type") == "turn_context":
                        model = ((rec.get("payload") or {}).get("model")) or ""
                        if model:
                            if not file_model:
                                file_model = model
                            if ts > newest_ctx_ts:
                                newest_ctx_ts, newest_ctx_model = ts, model
                        continue
                    payload = rec.get("payload") or {}
                    if payload.get("type") != "token_count":
                        continue
                    u = ((payload.get("info") or {}).get("last_token_usage")) or {}
                    if u:
                        cached = u.get("cached_input_tokens") or 0
                        fresh = max(0, (u.get("input_tokens") or 0) - cached)
                        events.append((ts, fresh, u.get("output_tokens") or 0, cached))
                    rl = payload.get("rate_limits") or {}
                    primary = rl.get("primary") or {}
                    if primary.get("used_percent") is not None and ts >= lim_ts:
                        lim_ts = ts
                        limits = {"w_pct": max(0, min(100, int(round(primary["used_percent"])))),
                                  "w_reset_epoch": primary.get("resets_at"),
                                  "w_window_min": primary.get("window_minutes") or 10080}
            files.append((file_model, events))
        except OSError:
            continue

    last_ev_ts, last_ev_model = 0.0, ""
    for file_model, events in files:
        model = file_model or newest_ctx_model  # no turn_context → machine's model
        for ts, fresh, output, cached in events:
            if ts > last_ev_ts:
                last_ev_ts, last_ev_model = ts, model
            cost = _codex_cost(model, fresh, output, cached)
            if cost <= 0:
                continue
            if ts >= week_cut:
                week += cost
            if ts >= today_cut:
                today += cost
            idx = 6 + int((ts - today_cut) // 86400)
            if 0 <= idx < 7:
                daily[idx] += cost

    # "In use now" = the model behind the most recent token event, but only if
    # it's recent (a session running now), else idle — same spirit as the Kimi chip.
    model = _codex_label(last_ev_model) if last_ev_ts and (now - last_ev_ts) <= 900 else ""
    return week, today, daily, model, limits


async def fetch_codex_usage() -> dict:
    """Codex fields for the payload, cached for CODEX_RECOMPUTE_S:
      cd/cdd   = week/today $ activity (API rates — a gauge, not a bill)
      cdw/cdwr = 7-day window % + reset (mins), from the newest rate_limits record
      cdx      = 7-day $ activity series for the sparkline (index 6 = today)
      cdm      = model in use now (e.g. "5.6 SOL"), "" when idle
    Returns {} (device hides the Codex view) only when there is no signal at all."""
    try:
        now = time.time()
        if now - _codex_cache["ts"] >= CODEX_RECOMPUTE_S:
            week, today, daily, model, lim = _recompute_codex()
            _codex_cache.update(ts=now, week=week, today=today, daily=daily, model=model)
            if lim:
                _codex_cache.update(lim)  # refresh last-good
        c = _codex_cache
        # Age the last-good limit off wall-clock: the countdown ticks down, and a
        # rolled window (reset epoch passed with no new log writes) zeroes the %
        # and advances to the next boundary — same projection as the Kimi view.
        w_pct, w_reset = _project_window(c["w_pct"], c["w_reset_epoch"], c["w_window_min"], now)
        if (c["week"] == 0 and c["today"] == 0
                and not w_pct and c["w_reset_epoch"] is None):
            return {}   # Codex never used and no limit ever read → no view
        return {
            "cd": round(c["week"]),
            "cdd": round(c["today"]),
            "cdw": w_pct,
            "cdwr": w_reset,
            "cdx": {"wk": [round(x, 4) for x in c["daily"]]},
            "cdm": c["model"],
        }
    except Exception as e:
        log(f"Codex usage compute failed: {e}")
        return {}


# --- Claude model-scoped weekly limit (Weekly-Fable / Weekly-Opus) ----------
# The Messages-API rate-limit headers expose ONLY the all-models 5h + 7d limits.
# The separate heavy-model weekly cap (the "Weekly Fable" 92% wall Luke sees on
# claude.ai) is NOT in those headers — a Fable/Opus request just 429s with no
# rate-limit headers at all. It IS available from /api/oauth/usage, the same
# endpoint Claude Code's own `/usage` panel reads, via a read-only GET with the
# OAuth token. `limits[]` carries one entry per limit; the model-scoped weekly is
# kind="weekly_scoped" and names its driving model (dynamic — Fable now, could be
# Opus another week), so the device label follows it honestly.
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


async def fetch_scoped_weekly(token: str) -> dict:
    """The most-binding active model-scoped weekly limit as {kw, kwr, kwm}, or {}
    when there is none / on any failure. Best-effort: a hiccup here must never
    block the Claude display (the 5h/7d limits come from poll_api regardless)."""
    headers = {
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-code/2.1.5",
        "Authorization": f"Bearer {token}",
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as http:
            resp = await http.get(CLAUDE_USAGE_URL, headers=headers)
        if resp.status_code != 200:
            return {}
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        log(f"scoped-weekly read failed: {e}")
        return {}
    except Exception as e:  # a usage-endpoint hiccup must never touch the Claude path
        log(f"scoped-weekly read error: {e}")
        return {}
    limits = data.get("limits") or []
    scoped = [l for l in limits
              if l.get("kind") == "weekly_scoped" and (l.get("percent") or 0) > 0]
    if not scoped:
        return {}
    scoped.sort(key=lambda l: l.get("percent") or 0, reverse=True)
    top = scoped[0]
    model = ((top.get("scope") or {}).get("model") or {}).get("display_name") or "WEEKLY"
    return {
        "kw": int(round(top.get("percent") or 0)),
        "kwr": _mins_until(_iso_to_epoch(top.get("resets_at"))),
        "kwm": str(model)[:12],
    }


# --- Claude Code transcript activity ($ spend, model split, in-use model) ----
# Mirrors PitCrew's ingestClaudeTranscripts()/estCost() (lib/usage.ts,
# usage-rates.ts) so the device agrees with the AI Usage tab, but self-computes
# here so nothing but this daemon has to be running. $ figures are "at API rates"
# — an activity gauge, not a bill (subscription is flat-rate; see [[ai-subscriptions]]).
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
# $ per 1M tokens (input, output, cacheRead, cacheWrite5m, cacheWrite1h). Anthropic
# only. Unlike Grok, cache_read is a SEPARATE field from input (not a subset), so no
# subtraction. Claude Code writes the 1h cache tier. Longest prefix first.
ANTHROPIC_RATES = [
    ("claude-fable-5",   (10.0, 50.0, 1.0,  12.5, 20.0)),
    ("claude-opus-4",    (5.0,  25.0, 0.5,  6.25, 10.0)),
    ("claude-sonnet",    (3.0,  15.0, 0.3,  3.75, 6.0)),
    ("claude-haiku-4-5", (1.0,  5.0,  0.1,  1.25, 2.0)),
]
# BY MODEL bar order must match proto's colours (Opus, Sonnet, Haiku, Fable).
CLAUDE_FAMILIES = ["opus", "sonnet", "haiku", "fable"]
CLAUDE_RECOMPUTE_S = 300
_claude_cache: dict = {"ts": 0.0}


def _claude_family(model: str) -> str:
    for fam in CLAUDE_FAMILIES:
        if fam in model:
            return fam
    return "other"


def _claude_label(model: str) -> str:
    """Short "IN USE" label from a model id, derived — not hardcoded, so new
    models Just Work. "claude-opus-5" → "OPUS 5", "claude-opus-4-8" → "OPUS 4.8".
    Never shows the "CLAUDE-" prefix (the Claude view already says it's Claude):
    the family is the first token, the ≤2-digit tokens after it are the version
    (a trailing YYYYMMDD snapshot date is dropped). Raw id if it doesn't parse."""
    m = model.lower()
    if m.startswith("claude-"):
        m = m[len("claude-"):]
    parts = m.split("-")
    if parts and parts[0].isalpha():
        fam = parts[0].upper()
        vers = []
        for p in parts[1:]:
            if p.isdigit() and len(p) <= 2:   # a version segment, not a date
                vers.append(p)
            else:
                break
        return (fam + " " + ".".join(vers)).strip()
    return model[:12].upper()


def _claude_cost(model: str, t: tuple) -> float:
    for prefix, (ci, co, cr, c5, c1) in ANTHROPIC_RATES:
        if model.startswith(prefix):
            return (t[0] * ci + t[1] * co + t[2] * cr + t[3] * c5 + t[4] * c1) / 1_000_000.0
    return 0.0


def _claude_tokens(u: dict) -> tuple:
    """(input, output, cacheRead, cacheWrite5m, cacheWrite1h) from an Anthropic usage
    blob. Uses the 5m/1h split when present, else buckets the aggregate as 5m."""
    cc = u.get("cache_creation")
    if isinstance(cc, dict):
        w5 = cc.get("ephemeral_5m_input_tokens") or 0
        w1 = cc.get("ephemeral_1h_input_tokens") or 0
    else:
        w5 = u.get("cache_creation_input_tokens") or 0
        w1 = 0
    return (u.get("input_tokens") or 0, u.get("output_tokens") or 0,
            u.get("cache_read_input_tokens") or 0, w5, w1)


def _recompute_claude() -> dict:
    """Scan ~/.claude/projects/**/*.jsonl for this week's Claude Code activity.
    One API response spans several JSONL lines sharing a message id, so dedupe by
    id. Returns today/week $, the per-family today split, a 7-day daily-$ series
    (index 6 = today), and the most-recent model in use."""
    now = time.time()
    week_cut = now - 7 * 86400
    lt = time.localtime(now)
    today_cut = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    week = today = 0.0
    by_family = {f: 0.0 for f in CLAUDE_FAMILIES}
    daily = [0.0] * 7
    seen: set = set()
    recent_cut = now - 600  # a model with a turn in the last 10 min = "in use"
    recent: dict = {}       # model label -> its latest ts within the window
    try:
        files = list(CLAUDE_PROJECTS.glob("**/*.jsonl"))
    except OSError:
        files = []
    for f in files:
        try:
            if f.stat().st_mtime < week_cut:
                continue
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    # cheap pre-filter before JSON.parse — most lines aren't turns
                    if '"type":"assistant"' not in line or '"usage"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("type") != "assistant":
                        continue
                    msg = rec.get("message") or {}
                    usage = msg.get("usage")
                    model = msg.get("model")
                    if not usage or not model or model == "<synthetic>":
                        continue
                    mid = msg.get("id") or rec.get("requestId") or rec.get("uuid")
                    ts = _iso_to_epoch(rec.get("timestamp"))
                    if not mid or ts is None or mid in seen:
                        continue
                    seen.add(mid)
                    cost = _claude_cost(model, _claude_tokens(usage))
                    if ts >= week_cut:
                        week += cost
                    if ts >= today_cut:
                        today += cost
                        by_family[_claude_family(model)] = \
                            by_family.get(_claude_family(model), 0.0) + cost
                    idx = 6 + int((ts - today_cut) // 86400)
                    if 0 <= idx < 7:
                        daily[idx] += cost
                    if ts >= recent_cut:
                        lbl = _claude_label(model)
                        if ts > recent.get(lbl, 0.0):
                            recent[lbl] = ts
        except OSError:
            continue
    # Models in use right now, most-recent first (parallel sessions → several).
    models = [lbl for lbl, _ in sorted(recent.items(), key=lambda kv: kv[1], reverse=True)][:3]
    return {
        "week": week,
        "today": today,
        "by": [round(by_family.get(f, 0.0), 4) for f in CLAUDE_FAMILIES],
        "daily": [round(x, 4) for x in daily],
        "models": models,
    }


def fetch_claude_extras() -> dict:
    """CLAUDE view extras nested under "cx", cached CLAUDE_RECOMPUTE_S:
      sp = today's $ activity   m = model in use   by = [opus,sonnet,haiku,fable] $
      wk = 7-day daily $ series (index 6 = today)
    Best-effort → {} on any failure so the three real limits are never affected."""
    try:
        now = time.time()
        if now - _claude_cache["ts"] >= CLAUDE_RECOMPUTE_S:
            _claude_cache.update(ts=now, **_recompute_claude())
        return {"cx": {
            "sp": round(_claude_cache.get("today", 0.0), 2),
            "mu": _claude_cache.get("models", []),
            "by": _claude_cache.get("by", [0, 0, 0, 0]),
            "wk": _claude_cache.get("daily", [0] * 7),
        }}
    except Exception as e:
        log(f"Claude extras compute failed: {e}")
        return {}


# --- Machine vitals via PitCrew's telemetry engine --------------------------
# scan-telemetry.ps1 is pure PowerShell (CIM + registry + nvidia-smi), standalone
# — no PitCrew server needed. The daemon runs it directly so vitals work whenever
# the daemon runs (the PitCrew web app is usually closed). Override the repo dir
# with CLAWDMETER_PITCREW_DIR; a missing script / non-Windows box just drops the
# vitals views rather than inventing numbers.
PITCREW_DIR = Path(os.environ.get("CLAWDMETER_PITCREW_DIR", Path.home() / "pitcrew"))
TELEMETRY_SCRIPT = PITCREW_DIR / "engine" / "scan-telemetry.ps1"
VITALS_RECOMPUTE_S = 30  # vitals refresh; full (non-Lite) scan adds ~1s for top procs
_vitals_cache: dict = {"ts": 0.0, "data": {}}


def _run_telemetry() -> dict:
    """Run scan-telemetry.ps1 (full, for the RAM top-process segments) and reshape
    its JSON into the compact vitals the device shows. {} on any failure (honest —
    a missing feed drops the view)."""
    if sys.platform != "win32" or not TELEMETRY_SCRIPT.exists():
        return {}
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File", str(TELEMETRY_SCRIPT)],
            capture_output=True, text=True, timeout=15,
            creationflags=CREATE_NO_WINDOW,   # no flashing console window
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return {}
        d = json.loads(proc.stdout).get("data") or {}
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        log(f"vitals scan failed: {e}")
        return {}
    cpu = d.get("cpu") or {}
    gpu = d.get("gpu") or {}
    ram = d.get("ram") or {}
    # Trim marketing suffixes so the short device font shows the useful part
    # ("AMD Ryzen 9 5900X", "NVIDIA RTX 3080 Ti") instead of a mid-word cut.
    cpu_name = re.sub(r"\s*\d+-Core Processor.*$", "", cpu.get("name") or "", flags=re.I)
    cpu_name = re.sub(r"\s*(Processor|CPU)\s*$", "", cpu_name, flags=re.I)
    cpu_name = cpu_name.replace("(R)", "").replace("(TM)", "").strip()
    gpu_name = (gpu.get("name") or "").replace("GeForce ", "").strip()
    out: dict = {
        "cpu": {
            "p": cpu.get("usagePct"),
            "n": cpu_name[:24],
            "clk": cpu.get("currentClockMHz"),
            "t": cpu.get("tempC"),                       # null → device shows "no sensor"
            "c": [int(x) for x in (cpu.get("perCore") or [])[:24]],
        },
        "ram": {
            "p": ram.get("pct"),
            "u": ram.get("usedBytes"),
            "tot": ram.get("totalBytes"),
        },
    }
    # Top memory-consuming processes → real segments for the RAM treemap (honest
    # subdivision of the used block; telemetry can't split it any other way).
    top_ram = ((d.get("top") or {}).get("ram")) or []
    segs = []
    for p in top_ram[:4]:
        b = p.get("bytes") or 0
        if b > 0:
            segs.append({"n": (p.get("name") or "")[:12], "b": int(b)})
    if segs:
        out["ram"]["seg"] = segs
    if gpu.get("present"):
        out["gpu"] = {
            "p": gpu.get("utilizationPct"),
            "n": gpu_name[:24],
            "t": gpu.get("tempC"),
            "pw": gpu.get("powerDrawW"),
            "pl": gpu.get("powerLimitW"),
            "vu": gpu.get("memUsedMB"),
            "vt": gpu.get("memTotalMB"),
        }
    return out


def read_vitals() -> dict:
    """Cached machine vitals for the payload ({cpu,ram[,gpu]}), or {} when the
    telemetry feed is unavailable so the device degrades to honest nulls."""
    now = time.time()
    if now - _vitals_cache["ts"] >= VITALS_RECOMPUTE_S:
        _vitals_cache.update(ts=now, data=_run_telemetry())
    return _vitals_cache["data"]


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

# --- Weather (open-meteo) ---------------------------------------------------
# No key, no auth, no account — one GET covers current conditions, the next 12
# hours, and today's sunrise/sunset. Cached WEATHER_RECOMPUTE_S; a failure
# returns {} so the view honestly shows no data rather than stale conditions.
# Location is Luke's (Old Lyme, CT 06371); override in the config file with
#   weather_lat = 41.3159
#   weather_lon = -72.3345
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
WEATHER_LAT, WEATHER_LON = 41.3159, -72.3345   # 06371
WEATHER_RECOMPUTE_S = 600
WEATHER_HOURS = 12
_weather_cache: dict = {"ts": 0.0, "data": {}}


def _config_float(key: str, default: float) -> float:
    """Read a float option from the config file; `default` on anything odd."""
    try:
        if CONFIG_FILE.exists():
            for line in CONFIG_FILE.read_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip().lower() == key:
                    return float(v.strip())
    except (OSError, ValueError):
        pass
    return default


def _hhmm(iso: str) -> str:
    """'2026-08-09T19:56' -> '7:56' (12-hour, no am/pm — the arc says which)."""
    try:
        h, m = iso.split("T")[1].split(":")[:2]
        h12 = int(h) % 12 or 12
        return f"{h12}:{m}"
    except (IndexError, ValueError):
        return ""


def _daylight_pct(now: float, sunrise: str, sunset: str) -> int:
    """How far through the daylight span we are, 0-100 (clamped at both ends).

    Drives the weather view's hero arc, so it must be honest before dawn and
    after dusk rather than wrapping around.
    """
    try:
        rise = datetime.datetime.fromisoformat(sunrise).timestamp()
        set_ = datetime.datetime.fromisoformat(sunset).timestamp()
    except (ValueError, OSError, OverflowError):
        return 0
    if set_ <= rise:
        return 0
    return max(0, min(100, int(round((now - rise) / (set_ - rise) * 100))))


async def fetch_weather() -> dict:
    """Current conditions + the next 12 hours under "wx". {} on any failure."""
    now = time.time()
    if now - _weather_cache["ts"] < WEATHER_RECOMPUTE_S:
        return dict(_weather_cache["data"])
    lat = _config_float("weather_lat", WEATHER_LAT)
    lon = _config_float("weather_lon", WEATHER_LON)
    params = {
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,apparent_temperature,weather_code,"
                   "relative_humidity_2m,wind_speed_10m,is_day",
        "hourly": "temperature_2m,precipitation_probability",
        "daily": "sunrise,sunset,temperature_2m_max,temperature_2m_min",
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
        "timezone": "auto", "forecast_days": 2,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.get(WEATHER_URL, params=params)
        if resp.status_code != 200:
            log(f"weather HTTP {resp.status_code}")
            return dict(_weather_cache["data"])   # hold last-good over a blip
        j = resp.json()
        cur, daily, hourly = j["current"], j["daily"], j["hourly"]
        # The hourly arrays start at midnight LOCAL today; find the current hour
        # and take the next WEATHER_HOURS from there.
        stamp = cur["time"][:13]          # 'YYYY-MM-DDTHH'
        i = next((k for k, t in enumerate(hourly["time"]) if t[:13] == stamp), 0)
        window = slice(i, i + WEATHER_HOURS)
        sunrise, sunset = daily["sunrise"][0], daily["sunset"][0]
        wx = {
            "t": round(cur["temperature_2m"]),
            "f": round(cur["apparent_temperature"]),
            "c": int(cur["weather_code"]),
            "d": int(cur["is_day"]),
            "h": round(cur["relative_humidity_2m"]),
            "w": round(cur["wind_speed_10m"]),
            "hi": round(daily["temperature_2m_max"][0]),
            "lo": round(daily["temperature_2m_min"][0]),
            "sr": _hhmm(sunrise), "ss": _hhmm(sunset),
            "dp": _daylight_pct(now, sunrise, sunset),
            # The clock hour the hourly list starts at, so the device can label
            # the columns without us shipping twelve strings.
            "h0": int(cur["time"][11:13]),
            "hr": [round(v) for v in hourly["temperature_2m"][window]],
            "pp": [round(v or 0) for v in hourly["precipitation_probability"][window]],
        }
    except Exception as e:
        log(f"weather fetch failed: {e}")
        return dict(_weather_cache["data"])
    _weather_cache.update(ts=now, data={"wx": wx})
    return {"wx": wx}


# --- Market (Yahoo chart/spark) ---------------------------------------------
# The spark endpoint batches every symbol into ONE request and returns, per
# symbol, the previous close, the live price and the intraday close series —
# exactly what the view needs. It is UNDOCUMENTED, so a failure returns {} and
# the view shows NO LIVE DATA rather than yesterday's prices dressed as today's.
#
# Luke's holdings roster comes from Monarch (end-of-day, so it prices nothing —
# it only says WHICH tickers he owns and how big each is). It changes rarely, so
# it lives in a sidecar written by tools/refresh_holdings.py rather than being
# re-fetched here. Only the top MARKET_TOP_N by value are priced: the long tail
# is sub-$2k and can't move the needle, and every extra symbol is more load on
# an endpoint nobody promised us.
MARKET_URL = "https://query1.finance.yahoo.com/v7/finance/spark"
MARKET_HEADERS = {"User-Agent": "Mozilla/5.0"}
MARKET_INDEXES = [("^GSPC", "S&P 500"), ("^IXIC", "NASDAQ"), ("^RUT", "RUSSELL")]
MARKET_TOP_N = 25
# Measured, not documented: the endpoint 400s at 21+ symbols in one call
# (20 is fine, 21 is not, and it is a cap rather than a bad ticker — the
# rejected tail returns 200 on its own). So the roster is sent in chunks.
MARKET_CHUNK = 20
MARKET_RECOMPUTE_S = 60
_HOLDINGS_SIDECAR = (Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
                     / "Clawdmeter" / "holdings.json")
_market_cache: dict = {"ts": 0.0, "data": {}}


def read_holdings() -> list[str]:
    """Tickers to price, biggest position first. [] if the roster was never seeded."""
    try:
        saved = json.loads(_HOLDINGS_SIDECAR.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = [r for r in saved.get("holdings", []) if r.get("ticker")]
    rows.sort(key=lambda r: -(r.get("value") or 0))
    return [r["ticker"] for r in rows[:MARKET_TOP_N]]


def _market_status(period: dict, now: float) -> tuple[str, int]:
    """('OPEN'|'CLOSED', minutes to the close / to the next open) from Yahoo's
    currentTradingPeriod. Pre/post are folded into CLOSED: the view is about the
    regular session, and a thin pre-market print is not a market being open."""
    try:
        reg = period["regular"]
        start, end = float(reg["start"]), float(reg["end"])
    except (KeyError, TypeError, ValueError):
        return "", -1
    if start <= now < end:
        return "OPEN", int((end - now) // 60)
    # Next open: today's start if we're before it, else roughly the next weekday.
    nxt = start if now < start else start + 86400
    while True:
        wd = datetime.datetime.fromtimestamp(nxt).weekday()
        if wd < 5:
            break
        nxt += 86400
    return "CLOSED", int(max(0, nxt - now) // 60)


async def fetch_market() -> dict:
    """Indexes, an intraday series for the hero, and today's top movers, as "mk".

    One batched request covers the three indexes plus the priced holdings.
    {} on any failure (undocumented endpoint — never show stale prices).
    """
    now = time.time()
    if now - _market_cache["ts"] < MARKET_RECOMPUTE_S:
        return dict(_market_cache["data"])
    holdings = read_holdings()
    symbols = [s for s, _ in MARKET_INDEXES] + holdings
    chunks = [symbols[i:i + MARKET_CHUNK] for i in range(0, len(symbols), MARKET_CHUNK)]
    results = []
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as http:
            for chunk in chunks:
                resp = await http.get(MARKET_URL, headers=MARKET_HEADERS,
                                      params={"symbols": ",".join(chunk),
                                              "range": "1d", "interval": "5m"})
                if resp.status_code != 200:
                    log(f"market HTTP {resp.status_code} for {len(chunk)} symbols")
                    continue   # a bad chunk costs those movers, not the whole view
                results.extend(resp.json()["spark"]["result"])
    except Exception as e:
        log(f"market fetch failed: {e}")
        return {}
    if not results:
        return {}

    by_symbol = {}
    for entry in results:
        try:
            r = entry["response"][0]
            meta = r["meta"]
            prev = meta.get("previousClose") or meta.get("chartPreviousClose")
            price = meta.get("regularMarketPrice")
            if not prev or price is None:
                continue
            closes = r.get("indicators", {}).get("quote", [{}])[0].get("close") or []
            by_symbol[entry["symbol"]] = {
                "price": float(price),
                "pct": (float(price) - float(prev)) / float(prev) * 100.0,
                "prev": float(prev),
                "closes": closes,
                "period": meta.get("currentTradingPeriod") or {},
            }
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    if not by_symbol:
        return {}

    mk: dict = {}
    mk["ix"] = [{"s": label, "p": round(by_symbol[sym]["price"], 2),
                 "c": round(by_symbol[sym]["pct"], 2)}
                for sym, label in MARKET_INDEXES if sym in by_symbol]
    hero = by_symbol.get(MARKET_INDEXES[0][0])
    if hero:
        # The intraday series is no longer drawn (the view is a stacked list, not
        # a chart), so it is not sent — it was ~200B of every payload.
        status, mins = _market_status(hero["period"], now)
        if status:
            mk["st"], mk["cd"] = status, mins
    # Top 3 movers by PERCENT (Luke's call) over the priced holdings only.
    movers = sorted((s for s in holdings if s in by_symbol),
                    key=lambda s: -abs(by_symbol[s]["pct"]))[:3]
    mk["mv"] = [{"s": s, "c": round(by_symbol[s]["pct"], 2),
                 "p": round(by_symbol[s]["price"], 2)} for s in movers]
    _market_cache.update(ts=now, data={"mk": mk})
    return {"mk": mk}


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
            "ol": 1,   # limits are LIVE (vs claude_limits_projection's "ol":0)
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
            "ol": 1,
        }
    add_chime_field(payload)   # adds "c":1 iff the config opts in
    add_clock_fields(payload)   # adds "t" + "tf" iff the config opts in
    payload.update(await fetch_grok_usage())  # adds "g"/"gd" iff PitCrew is up
    payload.update(await fetch_kimi_usage())  # adds "km"* iff Kimi has any signal
    payload.update(await fetch_codex_usage())  # adds "cd"* iff Codex has any signal
    payload.update(await fetch_weather())      # adds "wx" iff open-meteo answered
    payload.update(await fetch_market())       # adds "mk" iff the quote feed answered
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


# --- Mover brand logos over the serial link ---------------------------------
# Out of band from the usage payload: a 24x24 RGB565A8 logo is 2.3KB of base64,
# and the three on screen only change when the movers do. Re-sent when the set
# changes, and every LOGO_RESEND_S regardless — the device keeps them in RAM, so
# a reboot or a reflash must not leave it with blank rows until Luke happens to
# have a new top three.
LOGO_RESEND_S = 600
_logo_state: dict = {"syms": (), "ts": 0.0}


async def push_mover_logos(ser: "serial.Serial", payload: dict) -> None:
    """Send the current top movers' logos to the device. Best-effort throughout:
    a symbol we can't resolve simply has no logo, and the row still renders.

    The fetch runs in a worker thread. A cache miss is an HTTP round trip, and
    the asyncio loop is the sole owner of the serial port — it must not be
    parked on the network while a payload is due. Only the write happens here.
    """
    movers = [m.get("s") for m in payload.get("mk", {}).get("mv", []) if m.get("s")][:3]
    if not movers:
        return
    now = time.time()
    syms = tuple(movers)
    if syms == _logo_state["syms"] and now - _logo_state["ts"] < LOGO_RESEND_S:
        return

    def _load() -> list:
        out = []
        for sym in movers:
            try:
                out.append((sym, market_logos.fetch_logo(sym)))
            except Exception as e:
                log(f"logo {sym} failed: {e}")
                out.append((sym, None))
        return out

    logos = await asyncio.to_thread(_load)
    sent = 0
    for slot, (sym, data) in enumerate(logos):
        if not data:
            continue
        blob = base64.b64encode(data).decode("ascii")
        ser.write(f"mklogo {slot} {sym} {market_logos.LOGO_PX} {blob}\n".encode())
        ser.flush()          # a dead port raises — the main loop reopens it
        sent += 1
    _logo_state.update(syms=syms, ts=now)
    log(f"Logos pushed: {sent}/{len(movers)} for {', '.join(movers)}")


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


# ── Claude's 5h/7d limits: last-good + wall-clock projection ─────────────────
# Claude is the only provider whose limits come off the wire; the other three
# read local logs. The OAuth token that poll_api needs is refreshed only while
# Claude Code is RUNNING, so an idle desk means a run of 401s — and the Claude
# view was the one view that went blank precisely when you stopped using it,
# while Grok/Kimi/Codex all kept showing their last numbers.
#
# Same treatment Kimi already gets: keep the last live reading with its reset
# times as ABSOLUTE epochs, persist it so a daemon restart doesn't blank the
# view, and age it forward with _project_window() — the % holds and the
# countdown ticks until the window's reset passes, at which point it rolled
# over (you're idle, so usage is ~0) and the % zeroes. A live poll overwrites
# all of it. The payload flags which you're looking at with "ol".
_claude_limits_cache = {"s_pct": 0, "s_reset_epoch": None, "s_window_min": 300,
                 "w_pct": 0, "w_reset_epoch": None, "w_window_min": 10080,
                 "st": "allowed", "seen": False}
_CLAUDE_SIDECAR = (Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
                   / "Clawdmeter" / "claude_limits.json")
_CLAUDE_SIDECAR_KEYS = ("s_pct", "s_reset_epoch", "s_window_min",
                        "w_pct", "w_reset_epoch", "w_window_min", "st", "seen")


def _save_claude_sidecar() -> None:
    """Persist last-good limits so a daemon restart doesn't blank the Claude view."""
    try:
        _CLAUDE_SIDECAR.parent.mkdir(parents=True, exist_ok=True)
        _CLAUDE_SIDECAR.write_text(
            json.dumps({k: _claude_limits_cache[k] for k in _CLAUDE_SIDECAR_KEYS}), encoding="utf-8")
    except OSError:
        pass  # best-effort — persistence must never disturb the poll loop


def _load_claude_sidecar() -> None:
    """Seed the cache from the sidecar on startup (best-effort)."""
    try:
        saved = json.loads(_CLAUDE_SIDECAR.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for k in _CLAUDE_SIDECAR_KEYS:
        if k in saved and saved[k] is not None:
            _claude_limits_cache[k] = saved[k]


def remember_claude_limits(payload: dict) -> None:
    """Record a live 5h/7d reading as last-good, resets stored as absolute epochs.

    Pro/Max only: the Enterprise branch of poll_api reports a spending limit with
    a different shape ("w" is always 0), and projecting a monthly billing figure
    as if it were a rolling window would invent numbers.
    """
    if payload.get("acct") != "pro":
        return
    now = time.time()
    s_reset, w_reset = payload.get("sr"), payload.get("wr")
    _claude_limits_cache.update(
        s_pct=int(payload.get("s", 0)),
        w_pct=int(payload.get("w", 0)),
        st=payload.get("st", "allowed"),
        seen=True,
    )
    # A reset of 0/None means "unknown" from the header scrape — keep the epoch
    # we already have rather than pinning the countdown to now.
    if s_reset:
        _claude_limits_cache["s_reset_epoch"] = now + s_reset * 60
    if w_reset:
        _claude_limits_cache["w_reset_epoch"] = now + w_reset * 60
    _save_claude_sidecar()


def claude_limits_projection() -> dict:
    """Last-good 5h/7d limits aged off the wall clock; {} if none was ever seen."""
    if not _claude_limits_cache["seen"]:
        return {}
    now = time.time()
    c = _claude_limits_cache
    s_pct, s_reset = _project_window(c["s_pct"], c["s_reset_epoch"], c["s_window_min"], now)
    w_pct, w_reset = _project_window(c["w_pct"], c["w_reset_epoch"], c["w_window_min"], now)
    return {"s": s_pct, "sr": s_reset, "w": w_pct, "wr": w_reset,
            "st": c["st"], "acct": "pro", "ol": 0}


async def _send_local_only(ser: "serial.Serial") -> None:
    """Stream just the locally-computed fields (Grok/Kimi/Codex + vitals).

    The payload's Claude limits ride on the OAuth poll, so the old all-or-
    nothing path sent NOTHING while the Claude token was dead — and the desk
    gauge sat on its boot placeholders, which look like live numbers. The
    other providers read LOCAL logs (no token), so keep them flowing.

    Claude keeps flowing too, from two token-free sources: its 5h/7d limits are
    projected from the last live reading (claude_limits_projection, tagged
    "ol":0), and its $ / models-in-use / 7-day series come from the local
    transcripts, which never needed the token at all. Best-effort: a build
    failure sends nothing, same as before.
    """
    try:
        payload: dict = {}
        payload.update(claude_limits_projection())
        payload.update(fetch_claude_extras())
        payload.update(await fetch_grok_usage())
        payload.update(await fetch_kimi_usage())
        payload.update(await fetch_codex_usage())
        payload.update(await fetch_weather())
        payload.update(await fetch_market())
        payload.update(read_vitals())
    except Exception as e:
        log(f"local-only payload build failed: {e}")
        return
    if payload:
        write_payload(ser, payload)   # raises on a dead port — main loop reopens
        await push_mover_logos(ser, payload)


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
        await _send_local_only(ser)
        return
    try:
        payload = await poll_api(token)
    except AuthError:
        # Real 401/403 — token genuinely needs a refresh. Claude's fields die
        # with it; the local providers must keep streaming (see the helper).
        if tray_state:
            tray_state.set_error("token expired — run claude login")
        await _send_local_only(ser)
        return
    if payload is None:
        # Transient failure (network/DNS, timeout, rate-limit, 5xx). poll_api
        # already logged it; do NOT toast "token expired" (that mislabeled a
        # boot-time DNS blip as an auth problem, SC#5). Next poll retries.
        return
    remember_claude_limits(payload)   # last-good, for the next dead-token stretch
    # Phase-B real data — the model-scoped weekly limit (Fable/Opus), Claude
    # transcript activity, and machine vitals. All best-effort and merged here
    # (not in poll_api) so the proven 5h/7d + Grok path is untouched; any failure
    # returns {} and simply omits those fields rather than blocking the send.
    try:
        payload.update(await fetch_scoped_weekly(token))
        payload.update(fetch_claude_extras())
        payload.update(read_vitals())
    except Exception as e:
        log(f"payload augment failed: {e}")
    write_payload(ser, payload)   # raises serial.SerialException/OSError on a dead port
    await push_mover_logos(ser, payload)
    if tray_state:
        tray_state.set_connected(time.time())


def _next_backoff(current: int, cap: int) -> int:
    """Double `current`, clamped to `cap`. Pure helper for the port-retry backoff."""
    return min(current * 2, cap)


def _flush_view_cmds(ser: "serial.Serial") -> None:
    """Drain queued mouse view commands to the device. Runs only in the asyncio
    loop (same thread that owns `ser`), so it never races poll_and_send's write.
    A serial error is swallowed here — the next poll's write will hit it too and
    the main loop reopens the port."""
    while _view_cmds:
        cmd = _view_cmds.popleft()
        try:
            ser.write((cmd + "\n").encode())
            ser.flush()
            log(f"View cmd -> {cmd}")
        except (serial.SerialException, OSError) as e:
            log(f"View cmd write failed ({cmd}): {e}")
            return


def _run_mouse_hook(loop: "asyncio.AbstractEventLoop", view_event: "asyncio.Event") -> None:
    """Install a global WH_MOUSE_LL hook mapping the thumb side buttons to the
    device's view cycle, and pump messages. Enqueues "pprev"/"pnext" and wakes the
    asyncio loop to flush them; consumes the button events (they no longer act as
    browser Back/Forward). Best-effort: any failure just disables mouse control."""
    try:
        import ctypes
        from ctypes import wintypes
        WH_MOUSE_LL = 14
        WM_XBUTTONDOWN, WM_XBUTTONUP = 0x020B, 0x020C
        XBUTTON1, XBUTTON2 = 0x0001, 0x0002
        LRESULT = ctypes.c_ssize_t   # LONG_PTR — 64-bit on x64, so the hook chain
                                     # return value isn't truncated (the err-0 bug)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class MSLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]

        HOOKPROC = ctypes.CFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HMODULE, wintypes.DWORD]
        user32.CallNextHookEx.restype = LRESULT
        user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]

        def _cb(nCode, wParam, lParam):
            if nCode == 0 and wParam in (WM_XBUTTONDOWN, WM_XBUTTONUP):
                ms = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                btn = (ms.mouseData >> 16) & 0xFFFF
                if btn in (XBUTTON1, XBUTTON2):
                    if wParam == WM_XBUTTONDOWN:   # act on press, swallow the release
                        _view_cmds.append("pprev" if btn == XBUTTON1 else "pnext")
                        loop.call_soon_threadsafe(view_event.set)
                    return 1                        # consume — dedicate to the device
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        cb = HOOKPROC(_cb)
        _hook_refs.append(cb)   # pin against GC for the hook's lifetime
        hmod = kernel32.GetModuleHandleW(None)
        hook = user32.SetWindowsHookExW(WH_MOUSE_LL, cb, hmod, 0)
        if not hook:
            log(f"Mouse hook install failed (err {ctypes.get_last_error()})")
            return
        log("Mouse hook installed: XButton1->prev view, XButton2->next view")
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    except Exception as e:  # never let a hook problem take down the daemon
        log(f"Mouse hook thread error: {e}")


async def main(tray_state=None) -> None:
    stop_event = asyncio.Event()
    view_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    # Restore Claude's last-good limits before the first poll, so a restart while
    # the token is dead still streams numbers instead of blanking the view.
    _load_claude_sidecar()

    # Global mouse side-button -> view control (Windows only; harmless if it fails).
    threading.Thread(target=_run_mouse_hook, args=(loop, view_event),
                     name="mouse-hook", daemon=True).start()

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

    async def idle_servicing_mouse(secs: float, ser: "serial.Serial") -> None:
        """Sleep up to `secs` between polls, but wake the moment a mouse side
        button is pressed and flush the queued view command to the device — so a
        button press flips the view in ~ms, not up to a poll interval later."""
        deadline = loop.time() + secs
        while not stop_event.is_set():
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            try:
                await asyncio.wait_for(view_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return   # normal poll interval elapsed
            view_event.clear()
            _flush_view_cmds(ser)

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
                    # A fresh port means the device may have rebooted or been
                    # reflashed, and the logos live only in its RAM — forget what
                    # we think it has so the next payload re-pushes them.
                    _logo_state.update(syms=(), ts=0.0)
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

            await idle_servicing_mouse(POLL_INTERVAL, ser)
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
