"""Fresh prices and volume for the scanner from Alpaca's 15-minute-delayed SIP feed.

Free Alpaca accounts can read `delayed_sip` (all US exchanges, 15 minutes behind) through the
snapshot endpoint. We use it to overwrite price / volume / % change on the universe, so the
scanner is never more than ~15 minutes behind. If keys are missing or Alpaca fails, the
Nasdaq screener numbers already in the universe are left untouched.

Needs ALPACA_KEY and ALPACA_SECRET (a paper-trading key is enough).
Override the feed with ALPACA_SCAN_FEED (delayed_sip | iex | sip).
"""
from __future__ import annotations

import os
import time

import requests

from common import log

URL = "https://data.alpaca.markets/v2/stocks/snapshots"
CHUNK = 200


def parse_snapshot(s: dict) -> tuple[float | None, float | None, float | None]:
    """One Alpaca snapshot -> (price, volume, pct change vs previous close)."""
    price = (s.get("latestTrade") or {}).get("p")
    daily = s.get("dailyBar") or {}
    prev = (s.get("prevDailyBar") or {}).get("c")
    if price is None:
        price = daily.get("c")
    vol = daily.get("v")
    pct = (price - prev) / prev * 100 if price and prev else None
    return price, vol, pct


def refresh_quotes(uni: dict, key: str | None = None, secret: str | None = None,
                   feed: str | None = None) -> int:
    """Update price/volume/pct on every Co in `uni` in place. Returns how many were updated."""
    key = key or os.getenv("ALPACA_KEY")
    secret = secret or os.getenv("ALPACA_SECRET")
    feed = feed or os.getenv("ALPACA_SCAN_FEED") or "delayed_sip"
    if not (key and secret):
        log("alpaca: no ALPACA_KEY/ALPACA_SECRET, using Nasdaq screener prices")
        return 0
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    tickers = sorted(uni)
    updated = failed = 0
    for i in range(0, len(tickers), CHUNK):
        chunk = tickers[i : i + CHUNK]
        wire = {t.replace("-", "."): t for t in chunk}  # Alpaca writes BRK.B, Nasdaq writes BRK-B
        data = None
        for attempt in range(3):
            try:
                r = requests.get(URL, headers=headers, timeout=30,
                                 params={"symbols": ",".join(wire), "feed": feed})
                if r.status_code in (401, 403):
                    log(f"alpaca: {r.status_code} {r.text[:160]}. Check the key/secret and feed '{feed}'.")
                    return updated
                if r.status_code == 429 or r.status_code >= 500:
                    time.sleep(2 ** attempt)
                    continue
                r.raise_for_status()
                data = r.json()
                break
            except (requests.RequestException, ValueError) as e:
                log("alpaca chunk error:", e)
                time.sleep(2 ** attempt)
        if data is None:
            failed += 1
            continue
        for sym, snap in data.items():
            t = wire.get(sym)
            if not t or not isinstance(snap, dict):
                continue
            price, vol, pct = parse_snapshot(snap)
            if not price:
                continue
            co = uni[t]
            co.price = float(price)
            if vol is not None:
                co.volume = float(vol)
            if pct is not None:
                co.pct = round(pct, 3)
            updated += 1
        time.sleep(0.35)  # stay far under the free plan's request limit
    log(f"alpaca ({feed}): refreshed {updated}/{len(tickers)} tickers" + (f", {failed} chunks failed" if failed else ""))
    return updated
