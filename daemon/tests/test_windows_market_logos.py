#!/usr/bin/env python3
"""Brand-logo resolution and caching for the market view's top movers.

The contract worth pinning here is the CACHE, not the network: an early version
wrote a permanent "no logo for this ticker" entry whenever a lookup failed, so a
burst of requests that tripped Wikidata's rate limiter silently blacklisted MU
and MSFT forever. A poisoned negative cache is invisible — the row just never
grows a logo — so the transient/definitive split is tested directly.

Run: python -m pytest daemon/tests/test_windows_market_logos.py -x -q
"""
from unittest.mock import MagicMock, patch

import pytest

import daemon.market_logos as ml


@pytest.fixture(autouse=True)
def tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ml, "CACHE_DIR", tmp_path / "logos")
    monkeypatch.setattr(ml, "_last_query", [0.0])
    yield


def test_known_tickers_never_touch_the_network():
    """Luke's roster resolves from the bundled map — Wikidata is the fallback,
    not the hot path, because its public endpoint 429s a handful in a row."""
    with patch("httpx.get", side_effect=AssertionError("should not query")):
        assert ml.resolve_domain("MU") == ("micron.com", True)
        assert ml.resolve_domain("XLE") == ("ssga.com", True)   # ETF -> issuer


def test_rate_limited_lookup_is_transient_not_definitive():
    with patch("httpx.get", return_value=MagicMock(status_code=429)):
        domain, definitive = ml.resolve_domain("ZZZZ")
    assert domain is None and definitive is False


def test_empty_wikidata_answer_is_definitive():
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"results": {"bindings": []}}
    with patch("httpx.get", return_value=resp):
        domain, definitive = ml.resolve_domain("ZZZZ")
    assert domain is None and definitive is True


def test_regional_sites_lose_to_a_plain_com():
    """Wikidata lists micron.com.jp and micron.cn alongside micron.com."""
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"results": {"bindings": [
        {"site": {"value": "https://www.micron.com.jp"}},
        {"site": {"value": "https://www.micron.com/"}},
    ]}}
    with patch("httpx.get", return_value=resp):
        assert ml.resolve_domain("ZZZZ") == ("micron.com", True)


def test_transient_failure_is_not_cached_as_a_miss():
    """The bug that motivated the split: a 429 must leave no trace, so the next
    poll tries again instead of the ticker being blacklisted for good."""
    with patch("httpx.get", return_value=MagicMock(status_code=429)):
        assert ml.fetch_logo("ZZZZ") is None
    assert ml._load_cached("ZZZZ") is None


def test_definitive_miss_is_cached_so_we_stop_asking():
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"results": {"bindings": []}}
    with patch("httpx.get", return_value=resp):
        assert ml.fetch_logo("ZZZZ") is None
    assert ml._load_cached("ZZZZ") == {"ok": False, "px": ml.LOGO_PX}
    # ...and a second call must not go near the network again.
    with patch("httpx.get", side_effect=AssertionError("should not query")):
        assert ml.fetch_logo("ZZZZ") is None


def test_rgb565a8_layout_is_colour_plane_then_alpha():
    """LVGL reads px*px*2 colour bytes followed by px*px alpha bytes."""
    from PIL import Image
    img = Image.new("RGBA", (4, 4), (255, 0, 0, 128))
    out = ml._to_rgb565a8(img, 4)
    assert len(out) == 4 * 4 * 3
    assert out[0:2] == bytes((0x00, 0xF8))       # pure red, little-endian RGB565
    assert set(out[32:]) == {128}                # the alpha plane


def test_logo_bytes_are_cached_on_disk_and_reused():
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGBA", (8, 8), (0, 0, 255, 255)).save(buf, format="PNG")
    resp = MagicMock(status_code=200, content=buf.getvalue())
    with patch("httpx.get", return_value=resp):
        first = ml.fetch_logo("MU")
    assert first and len(first) == ml.LOGO_PX * ml.LOGO_PX * 3
    with patch("httpx.get", side_effect=AssertionError("should not refetch")):
        assert ml.fetch_logo("MU") == first
