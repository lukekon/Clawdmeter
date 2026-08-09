#!/usr/bin/env python3
"""Seed the market view's holdings roster from a Monarch portfolio export.

The device's market view needs to know WHICH tickers Luke owns and roughly how
big each position is. Monarch is the right source for that, but its prices are
end-of-day, so it is used only for the roster — live prices and today's moves
come from Yahoo inside the daemon.

The roster changes rarely (a trade, not a tick), so it lives in a sidecar the
daemon reads rather than being fetched on the poll loop. Refresh it by asking
Claude to re-pull holdings from Monarch and re-running this against the saved
JSON, or by hand-editing the sidecar.

Non-tradeable rows are dropped: loans, fixed income and cash have no ticker
Yahoo can price, and one of them is 47% of the book — leaving it in would put a
permanent dash on the screen.

Usage: .venv/Scripts/python.exe tools/refresh_holdings.py <monarch_holdings.json>
Writes: %LOCALAPPDATA%\\Clawdmeter\\holdings.json
"""

import json
import os
import sys
from pathlib import Path

OUT = (Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
       / "Clawdmeter" / "holdings.json")
TRADEABLE = {"etf", "equity", "mutual_fund"}


def extract(raw: str) -> list[dict]:
    """Pull (ticker, value) pairs out of a Monarch get_account_holdings result.

    The payload arrives double-encoded (a JSON string inside {"result": ...})
    when it comes from the MCP tool, and plain when hand-saved — accept both.
    """
    doc = json.loads(raw)
    if isinstance(doc, dict) and "result" in doc and isinstance(doc["result"], str):
        doc = json.loads(doc["result"])
    edges = doc["portfolio"]["aggregateHoldings"]["edges"]
    rows = []
    for edge in edges:
        node = edge["node"]
        sec = node.get("security") or {}
        ticker, value = sec.get("ticker"), node.get("totalValue") or 0
        if ticker and value > 0 and (sec.get("type") or "") in TRADEABLE:
            rows.append({"ticker": ticker, "value": round(value, 2)})
    rows.sort(key=lambda r: -r["value"])
    return rows


def main() -> None:
    rows = extract(Path(sys.argv[1]).read_text(encoding="utf-8"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"holdings": rows}, indent=1), encoding="utf-8")
    total = sum(r["value"] for r in rows)
    top = rows[:25]
    print(f"wrote {OUT}: {len(rows)} tradeable positions, ${total:,.0f}")
    print(f"top 25 priced by the daemon = {sum(r['value'] for r in top) / total * 100:.0f}% "
          f"of tradeable value")
    print("  " + " ".join(r["ticker"] for r in top))


if __name__ == "__main__":
    main()
