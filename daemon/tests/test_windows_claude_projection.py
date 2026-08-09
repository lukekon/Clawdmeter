#!/usr/bin/env python3
"""Claude last-good limits + wall-clock projection.

The Claude view was the only one that went blank when Luke stopped using
Claude: its 5h/7d limits need a live OAuth token, and Claude Code refreshes
that token only while it runs. These tests pin the behaviour that fixes it —
remember a live reading, age it off the wall clock, roll the window over when
its reset passes, and never invent numbers before the first live reading.

Run: python -m pytest daemon/tests/test_windows_claude_projection.py -x -q
"""
import json
import time

import pytest

import daemon.claude_usage_daemon_windows as d


@pytest.fixture(autouse=True)
def clean_cache(tmp_path, monkeypatch):
    """Fresh cache + a throwaway sidecar path for every test."""
    monkeypatch.setattr(d, "_CLAUDE_SIDECAR", tmp_path / "claude_limits.json")
    monkeypatch.setattr(d, "_claude_limits_cache", {
        "s_pct": 0, "s_reset_epoch": None, "s_window_min": 300,
        "w_pct": 0, "w_reset_epoch": None, "w_window_min": 10080,
        "st": "allowed", "seen": False,
    })
    yield


def _live(s=40, sr=120, w=60, wr=3000, acct="pro"):
    return {"s": s, "sr": sr, "w": w, "wr": wr, "st": "allowed",
            "acct": acct, "ok": True, "ol": 1}


def test_no_projection_before_any_live_reading():
    """A fresh install must show NO LIVE DATA, not a made-up 0%."""
    assert d.claude_limits_projection() == {}


def test_projection_holds_pct_and_ticks_the_countdown(monkeypatch):
    d.remember_claude_limits(_live(s=40, sr=120))
    monkeypatch.setattr(d.time, "time", lambda: _now + 30 * 60)
    out = d.claude_limits_projection()
    assert out["s"] == 40          # % holds — nothing consumed it
    assert out["sr"] == pytest.approx(90, abs=1)   # countdown ticked off wall-clock
    assert out["ol"] == 0          # flagged as projected, not live


def test_projection_zeroes_and_rolls_over_after_the_reset(monkeypatch):
    d.remember_claude_limits(_live(s=40, sr=120))
    monkeypatch.setattr(d.time, "time", lambda: _now + 130 * 60)
    out = d.claude_limits_projection()
    assert out["s"] == 0                            # window rolled while idle
    assert out["sr"] == pytest.approx(290, abs=1)   # next 5h boundary


def test_projection_skips_several_missed_windows(monkeypatch):
    """Daemon off for a day → advance to the NEXT boundary, not a stale one."""
    d.remember_claude_limits(_live(s=40, sr=60))
    monkeypatch.setattr(d.time, "time", lambda: _now + 24 * 3600)
    out = d.claude_limits_projection()
    assert out["s"] == 0
    assert 0 <= out["sr"] <= 300


def test_sidecar_survives_a_restart(monkeypatch):
    d.remember_claude_limits(_live(s=55, sr=90, w=71, wr=4000))
    saved = json.loads(d._CLAUDE_SIDECAR.read_text(encoding="utf-8"))
    assert saved["s_pct"] == 55 and saved["w_pct"] == 71 and saved["seen"] is True
    # Simulate a restart: cache wiped, sidecar reloaded.
    d._claude_limits_cache.update(s_pct=0, w_pct=0, s_reset_epoch=None,
                                  w_reset_epoch=None, seen=False)
    d._load_claude_sidecar()
    assert d.claude_limits_projection()["s"] == 55


def test_enterprise_readings_are_not_remembered():
    """The ent branch reports a monthly spend figure; projecting it as a rolling
    window would invent numbers."""
    d.remember_claude_limits(_live(acct="ent"))
    assert d.claude_limits_projection() == {}


def test_unknown_reset_does_not_pin_the_countdown_to_now(monkeypatch):
    """A 0/absent reset header means 'unknown' — keep the epoch we had."""
    d.remember_claude_limits(_live(s=30, sr=120))
    before = d._claude_limits_cache["s_reset_epoch"]
    d.remember_claude_limits(_live(s=35, sr=0))
    assert d._claude_limits_cache["s_reset_epoch"] == before
    assert d._claude_limits_cache["s_pct"] == 35


_now = time.time()


@pytest.fixture(autouse=True)
def freeze_now(monkeypatch):
    """Pin 'now' for remember_claude_limits so tests can step time forward."""
    global _now
    _now = time.time()
    monkeypatch.setattr(d.time, "time", lambda: _now)
    yield
