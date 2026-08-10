#!/usr/bin/env python3
"""Brand logos for the market view's top movers.

The device cannot fetch these itself, and they cannot be baked into flash:
today's three biggest movers are any three of Luke's ~25 priced positions, and
the roster changes whenever he trades. So the daemon resolves them and streams
the three current ones to the device over the serial link.

Resolution, all free and key-less:

  ticker -> company website   KNOWN_DOMAINS first (covers Luke's roster; no
                              network at all), then Wikidata SPARQL as the
                              fallback for a ticker he buys later. Wikidata is
                              NOT the primary path: its public endpoint 429s a
                              handful of lookups in a row.
  website -> icon             Google's favicon service. Favicons are designed
                              to survive being tiny, which is exactly the job
                              at 28px; a full logotype would be a smear.

Everything is cached on disk under %LOCALAPPDATA%\\Clawdmeter\\logos, keyed by
ticker, so a symbol costs two HTTP calls once and nothing ever again. A ticker
that cannot be resolved is cached as a negative result so we stop asking.

Output format is what LVGL wants: RGB565A8 — px*px little-endian RGB565 colour
bytes followed by px*px alpha bytes.
"""

import json
import os
from pathlib import Path

import httpx

LOGO_PX = 28
CACHE_DIR = (Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
             / "Clawdmeter" / "logos")
WIKIDATA_URL = "https://query.wikidata.org/sparql"
FAVICON_URL = "https://www.google.com/s2/favicons"
UA = "Clawdmeter/1.0 (personal desk display)"

# Known ticker -> domain. This is the PRIMARY path and Wikidata is the fallback,
# not the other way round: query.wikidata.org is a shared public endpoint that
# 429s a handful of lookups in a row and stays angry for minutes afterwards —
# fine for a one-off script, wrong to put a polling daemon's behaviour behind.
# Everything below covers Luke's priced roster, and every domain here was checked
# to return a real favicon rather than the service's default globe.
#
# ETFs and trusts are mapped to their ISSUER: a VanEck fund showing VanEck's mark
# is the honest answer, since the fund itself has no brand of its own.
KNOWN_DOMAINS = {
    # issuers
    "XLB": "ssga.com", "XLC": "ssga.com", "XLE": "ssga.com", "XLF": "ssga.com",
    "XLI": "ssga.com", "XLK": "ssga.com", "XLP": "ssga.com", "XLU": "ssga.com",
    "XLV": "ssga.com", "XLY": "ssga.com", "MDY": "ssga.com",
    "QQQ": "invesco.com", "IWM": "ishares.com", "IBIT": "ishares.com",
    "NLR": "vaneck.com", "EPI": "wisdomtree.com", "VBR": "vanguard.com",
    "SPCX": "spacex.com",     # private-market tracker
    # operating companies
    "AAPL": "apple.com", "AMZN": "amazon.com", "ASML": "asml.com",
    "AVGO": "broadcom.com", "CAVA": "cava.com", "COST": "costco.com",
    "GOOG": "abc.xyz", "INTU": "intuit.com", "JPM": "jpmorganchase.com",
    "LLY": "lilly.com", "MCK": "mckesson.com", "META": "meta.com",
    "MSFT": "microsoft.com", "MU": "micron.com", "NFLX": "netflix.com",
    "NVDA": "nvidia.com", "RCL": "royalcaribbean.com", "SCHW": "schwab.com",
    "TOL": "tollbrothers.com", "ULTA": "ulta.com", "UNH": "unitedhealthgroup.com",
    "V": "visa.com",
}


def _cache_path(ticker: str) -> Path:
    return CACHE_DIR / f"{ticker.upper()}.json"


def _load_cached(ticker: str) -> dict | None:
    try:
        return json.loads(_cache_path(ticker).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_cached(ticker: str, entry: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(ticker).write_text(json.dumps(entry), encoding="utf-8")
    except OSError:
        pass   # a cache we can't write is slow, not broken


_last_query = [0.0]          # module-level so the spacing survives calls
WIKIDATA_MIN_GAP = 2.0       # seconds; the endpoint 429s a tight loop


def resolve_domain(ticker: str) -> tuple[str | None, bool]:
    """Ticker -> (domain, definitive).

    `definitive` distinguishes "Wikidata answered and has nothing for this
    ticker" from "we could not ask" (429, timeout, DNS). Only the former may be
    cached as a failure — an early version blacklisted MU and MSFT permanently
    because a burst of lookups tripped the rate limiter, which is exactly the
    kind of poisoned cache that is invisible until you wonder why a logo never
    shows up.
    """
    import time
    if ticker.upper() in KNOWN_DOMAINS:
        return KNOWN_DOMAINS[ticker.upper()], True
    query = (
        'SELECT ?site WHERE {'
        f'  ?item p:P414 ?ex . ?ex pq:P249 "{ticker.upper()}" .'
        '  ?item wdt:P856 ?site } LIMIT 5'
    )
    gap = time.time() - _last_query[0]
    if gap < WIKIDATA_MIN_GAP:
        time.sleep(WIKIDATA_MIN_GAP - gap)
    _last_query[0] = time.time()
    try:
        r = httpx.get(WIKIDATA_URL, params={"query": query, "format": "json"},
                      headers={"User-Agent": UA, "Accept": "application/sparql-results+json"},
                      timeout=25)
        if r.status_code != 200:
            return None, False           # rate-limited or down — ask again later
        rows = r.json()["results"]["bindings"]
    except Exception:
        return None, False
    hosts = []
    for row in rows:
        host = row["site"]["value"].split("//", 1)[-1].split("/", 1)[0].lower()
        hosts.append(host[4:] if host.startswith("www.") else host)
    # Wikidata lists regional sites too (micron.com.jp, apple.com.cn); prefer a
    # plain .com, else take whatever came first.
    for host in hosts:
        if host.endswith(".com") and host.count(".") == 1:
            return host, True
    return (hosts[0], True) if hosts else (None, True)


def _to_rgb565a8(img, px: int) -> bytes:
    """PIL image -> RGB565A8 (colour plane then alpha plane), as LVGL expects."""
    from PIL import Image
    img = img.convert("RGBA").resize((px, px), Image.LANCZOS)
    raw = img.tobytes()                          # RGBA, row-major (getdata() is
    colour = bytearray()                         # deprecated in Pillow 14)
    alpha = bytearray()
    for i in range(0, len(raw), 4):
        r, g, b, a = raw[i], raw[i + 1], raw[i + 2], raw[i + 3]
        v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        colour += bytes((v & 0xFF, v >> 8))      # little-endian
        alpha.append(a)
    return bytes(colour + alpha)


def fetch_logo(ticker: str, px: int = LOGO_PX) -> bytes | None:
    """RGB565A8 bytes for a ticker's brand mark, or None. Cached on disk."""
    cached = _load_cached(ticker)
    if cached is not None and cached.get("px") == px:
        if not cached.get("ok"):
            return None                          # negative result — stop asking
        raw = CACHE_DIR / f"{ticker.upper()}.bin"
        try:
            data = raw.read_bytes()
            if len(data) == px * px * 3:
                return data
        except OSError:
            pass

    domain = (cached or {}).get("domain")
    if not domain:
        domain, definitive = resolve_domain(ticker)
        if not domain:
            # Only remember the miss when Wikidata actually answered "nothing".
            if definitive:
                _save_cached(ticker, {"ok": False, "px": px})
            return None
    try:
        from PIL import Image
        import io
        r = httpx.get(FAVICON_URL, params={"domain": domain, "sz": 128},
                      headers={"User-Agent": UA}, timeout=20, follow_redirects=True)
        if r.status_code != 200 or not r.content:
            return None                  # transient — keep the domain, retry later
        data = _to_rgb565a8(Image.open(io.BytesIO(r.content)), px)
    except Exception:
        return None
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / f"{ticker.upper()}.bin").write_bytes(data)
    except OSError:
        pass
    _save_cached(ticker, {"ok": True, "px": px, "domain": domain})
    return data
