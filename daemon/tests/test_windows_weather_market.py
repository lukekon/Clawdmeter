#!/usr/bin/env python3
"""The two non-AI feeds: weather (open-meteo) and market (Yahoo spark).

Both talk to endpoints nobody promised us, so the contract these tests pin is
mostly about FAILING honestly — a market view showing yesterday's prices as if
they were live is worse than one showing nothing.

Run: python -m pytest daemon/tests/test_windows_weather_market.py -x -q
"""
import time
from unittest.mock import MagicMock, patch

import pytest

import daemon.claude_usage_daemon_windows as d


def _run(coro):
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── weather ──────────────────────────────────────────────────────────────────

def test_daylight_pct_spans_sunrise_to_sunset():
    rise, set_ = "2026-08-09T06:00", "2026-08-09T18:00"
    noon = time.mktime(time.strptime("2026-08-09 12:00", "%Y-%m-%d %H:%M"))
    assert d._daylight_pct(noon, rise, set_) == 50


def test_daylight_pct_clamps_outside_daylight():
    """Before dawn and after dusk must not wrap around — the arc would lie."""
    rise, set_ = "2026-08-09T06:00", "2026-08-09T18:00"
    dawn = time.mktime(time.strptime("2026-08-09 03:00", "%Y-%m-%d %H:%M"))
    night = time.mktime(time.strptime("2026-08-09 23:00", "%Y-%m-%d %H:%M"))
    assert d._daylight_pct(dawn, rise, set_) == 0
    assert d._daylight_pct(night, rise, set_) == 100


def test_hhmm_is_12_hour():
    assert d._hhmm("2026-08-09T19:56") == "7:56"
    assert d._hhmm("2026-08-09T00:07") == "12:07"
    assert d._hhmm("garbage") == ""


# ── market ───────────────────────────────────────────────────────────────────

def test_market_status_open_counts_down_to_the_close():
    now = 1000.0
    period = {"regular": {"start": 940.0, "end": 1000.0 + 3600}}
    assert d._market_status(period, now) == ("OPEN", 60)


def test_market_status_skips_the_weekend_when_shut():
    """Saturday evening must count to Monday's open, not to 'tomorrow'."""
    sat = time.mktime(time.strptime("2026-08-08 20:00", "%Y-%m-%d %H:%M"))
    start = time.mktime(time.strptime("2026-08-08 09:30", "%Y-%m-%d %H:%M"))
    status, mins = d._market_status({"regular": {"start": start, "end": start + 23400}}, sat)
    assert status == "CLOSED"
    opens_at = sat + mins * 60
    assert time.localtime(opens_at).tm_wday == 0     # Monday


@patch.object(d, "read_holdings", return_value=[])
def test_market_returns_nothing_when_the_endpoint_fails(_holdings):
    """No stale prices, ever: a bad response yields {} and the view goes dark."""
    d._market_cache.update(ts=0.0, data={})
    resp = MagicMock(status_code=500)
    with patch("httpx.AsyncClient") as client:
        client.return_value.__aenter__.return_value.get = _async(resp)
        assert _run(d.fetch_market()) == {}


def _async(value):
    async def _call(*_a, **_kw):
        return value
    return _call


@patch.object(d, "read_holdings", return_value=[])
def test_market_chunks_requests_under_the_symbol_cap(_holdings, monkeypatch):
    """Yahoo 400s at 21+ symbols per call — measured, not documented."""
    monkeypatch.setattr(d, "MARKET_INDEXES", [(f"S{i}", f"N{i}") for i in range(45)])
    seen = []

    async def _get(_url, headers=None, params=None):
        seen.append(params["symbols"].split(","))
        return MagicMock(status_code=500)     # content doesn't matter here

    d._market_cache.update(ts=0.0, data={})
    with patch("httpx.AsyncClient") as client:
        client.return_value.__aenter__.return_value.get = _get
        _run(d.fetch_market())
    assert seen and all(len(c) <= d.MARKET_CHUNK for c in seen)
    assert sum(len(c) for c in seen) == 45
