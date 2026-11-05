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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from common import log

ET = ZoneInfo("America/New_York")
URL = "https://data.alpaca.markets/v2/stocks/snapshots"
BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
CHUNK = 200


def session_of(now_et: datetime) -> str:
    """pre (4:00-9:30), regular, after (16:00-20:00), else closed (overnight / weekend)."""
    if now_et.weekday() >= 5:
        return "closed"
    m = now_et.hour * 60 + now_et.minute
    if 4 * 60 <= m < 9 * 60 + 30:
        return "pre"
    if 9 * 60 + 30 <= m < 16 * 60:
        return "regular"
    if 16 * 60 <= m < 20 * 60:
        return "after"
    return "closed"


def _bar_date(bar: dict | None):
    t = (bar or {}).get("t")
    if not t:
        return None
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(ET).date()
    except ValueError:
        return None


def parse_snapshot(s: dict, now_et: datetime | None = None) -> tuple[float | None, float | None, float | None]:
    """One Alpaca snapshot -> (price, volume today or None, % move vs the right reference close).

    Reference close: during the regular session it is yesterday's close. Before the open it is the
    last completed close (Alpaca's dailyBar is still yesterday then). After 16:00 it is today's
    close, so after-hours % is the extended-hours move, not the whole day."""
    price = (s.get("latestTrade") or {}).get("p")
    daily = s.get("dailyBar") or {}
    prevd = s.get("prevDailyBar") or {}
    if price is None:
        price = daily.get("c")
    d_date = _bar_date(daily)
    if now_et is None:
        ref, vol = prevd.get("c"), daily.get("v")
    else:
        today = now_et.date()
        after_close = now_et.hour * 60 + now_et.minute >= 16 * 60
        if d_date == today and not after_close:
            ref = prevd.get("c")
        else:
            ref = daily.get("c")
        vol = daily.get("v") if d_date == today else None
    pct = (price - ref) / ref * 100 if price and ref else None
    return price, vol, pct


def refresh_quotes(uni: dict, key: str | None = None, secret: str | None = None,
                   feed: str | None = None, now_et: datetime | None = None) -> int:
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
            price, vol, pct = parse_snapshot(snap, now_et)
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


ASSETS_URL = "https://paper-api.alpaca.markets/v2/assets"
LISTED = {"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS", "NYSEARCA"}


def tradable_symbols(key: str | None = None, secret: str | None = None) -> set[str] | None:
    """Tickers that are active, tradable and on a real exchange (no OTC). None = could not check."""
    key = key or os.getenv("ALPACA_KEY")
    secret = secret or os.getenv("ALPACA_SECRET")
    if not (key and secret):
        return None
    try:
        r = requests.get(ASSETS_URL, timeout=60,
                         headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
                         params={"status": "active", "asset_class": "us_equity"})
        r.raise_for_status()
        out = {a["symbol"].replace(".", "-") for a in r.json()
               if a.get("tradable") and (a.get("exchange") or "").upper() in LISTED}
    except (requests.RequestException, ValueError, KeyError) as e:
        log("alpaca assets unavailable:", e)
        return None
    return out or None


# ---------------- extended-hours volume, daily bars, chart reads ----------------
def _get_json(headers: dict, params: dict, url: str = BARS_URL) -> dict | None:
    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=45)
            if r.status_code in (401, 403):
                log(f"alpaca: {r.status_code} {r.text[:120]}")
                return None
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            log("alpaca bars error:", e)
            time.sleep(2 ** attempt)
    return None


def _fetch_bars(symbols: list[str], timeframe: str, start: str, headers: dict,
                feed: str, adjustment: str = "raw") -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for i in range(0, len(symbols), 100):
        wire = {t.replace("-", "."): t for t in symbols[i : i + 100]}
        token = None
        for _page in range(20):
            params = {"symbols": ",".join(wire), "timeframe": timeframe, "start": start,
                      "feed": feed, "limit": 10000, "adjustment": adjustment}
            if token:
                params["page_token"] = token
            data = _get_json(headers, params)
            if data is None:
                break
            for sym, bars in (data.get("bars") or {}).items():
                t = wire.get(sym)
                if t:
                    out.setdefault(t, []).extend(bars)
            token = data.get("next_page_token")
            if not token:
                break
        time.sleep(0.35)
    return out


def _auth():
    key, secret = os.getenv("ALPACA_KEY"), os.getenv("ALPACA_SECRET")
    if not (key and secret):
        return None, None
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}, os.getenv("ALPACA_SCAN_FEED") or "delayed_sip"


def refresh_volumes(uni: dict, now_et: datetime, min_abs_pct: float = 3.0) -> int:
    """Today's total volume (pre-market + regular + after-hours) for tickers that are moving.
    The snapshot's daily volume is yesterday's before the open, so it cannot be trusted then."""
    headers, feed = _auth()
    if not headers or now_et.weekday() >= 5 or now_et.hour < 4:
        return 0
    cands = sorted(t for t, c in uni.items() if c.price and c.pct is not None and abs(c.pct) >= min_abs_pct)
    if not cands:
        return 0
    start = now_et.replace(hour=4, minute=0, second=0, microsecond=0).isoformat()
    bars = _fetch_bars(cands, "1Hour", start, headers, feed)
    n = 0
    for t, bs in bars.items():
        total = sum(float(b.get("v") or 0) for b in bs)
        if total > 0:
            uni[t].volume = total
            n += 1
    log(f"alpaca: today's extended-hours volume for {n}/{len(cands)} moving tickers")
    return n


def _ema(vals: list[float], n: int) -> list[float]:
    k, e = 2 / (n + 1), vals[0]
    out = [e]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def _rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) <= n:
        return None
    g = l = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        g += max(d, 0)
        l += max(-d, 0)
    ag, al = g / n, l / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def _atr_pct(bars: list[dict], n: int = 14) -> float | None:
    trs = [max(bars[i]["h"] - bars[i]["l"], abs(bars[i]["h"] - bars[i - 1]["c"]),
               abs(bars[i]["l"] - bars[i - 1]["c"])) for i in range(1, len(bars))]
    if len(trs) < n:
        return None
    atr = sum(trs[:n]) / n
    for tr in trs[n:]:
        atr = (atr * (n - 1) + tr) / n
    return atr / bars[-1]["c"] * 100


def technicals(bars: list[dict]) -> dict | None:
    """Plain chart read from daily bars: trend, momentum, volatility, levels. Not a prediction."""
    if not bars or len(bars) < 30:
        return None
    c = [float(b["c"]) for b in bars]
    last = c[-1]
    s20 = sum(c[-20:]) / 20
    s50 = sum(c[-50:]) / 50 if len(c) >= 50 else None
    rsi = _rsi(c)
    macd = [a - b for a, b in zip(_ema(c, 12), _ema(c, 26))]
    hist = macd[-1] - _ema(macd, 9)[-1]
    v = [float(b.get("v") or 0) for b in bars]
    base = sum(v[-25:-5]) / 20 if len(v) >= 25 else 0
    vt = (sum(v[-5:]) / 5) / base if base > 0 else None
    score, notes = 0, []
    if last > s20:
        score += 1
        notes.append("above 20-day average")
    else:
        score -= 1
        notes.append("below 20-day average")
    if s50 is not None:
        if last > s50:
            score += 1
            notes.append("above 50-day average")
        else:
            score -= 1
            notes.append("below 50-day average")
        if s20 > s50:
            score += 1
        else:
            score -= 1
    if hist > 0:
        score += 1
        notes.append("MACD turning up")
    else:
        score -= 1
        notes.append("MACD turning down")
    if rsi is not None:
        if 50 <= rsi <= 70:
            score += 1
            notes.append(f"RSI {rsi:.0f} (healthy momentum)")
        elif rsi < 40:
            score -= 1
            notes.append(f"RSI {rsi:.0f} (weak)")
        elif rsi > 80:
            notes.append(f"RSI {rsi:.0f} (overbought, can snap back)")
    if vt is not None and vt > 1.3 and last > s20:
        score += 1
        notes.append("volume rising")
    lean = "BULLISH" if score >= 3 else "BEARISH" if score <= -3 else "NEUTRAL"
    return {"lean": lean, "score": score, "notes": notes, "rsi": None if rsi is None else round(rsi, 1),
            "atr_pct": None if _atr_pct(bars) is None else round(_atr_pct(bars), 1),
            "support": round(min(float(b["l"]) for b in bars[-20:]), 4),
            "resistance": round(max(float(b["h"]) for b in bars[-20:]), 4)}


def fetch_techs(tickers: list[str]) -> dict[str, dict]:
    """Chart read for each ticker (daily bars, 15-min delayed SIP). {} without keys."""
    headers, feed = _auth()
    tickers = sorted(set(tickers))
    if not headers or not tickers:
        return {}
    start = (datetime.now(ET) - timedelta(days=150)).date().isoformat()
    bars = _fetch_bars(tickers, "1Day", start, headers, feed, adjustment="split")
    out = {}
    for t, bs in bars.items():
        tc = technicals(sorted(bs, key=lambda b: b["t"]))
        if tc:
            out[t] = tc
    log(f"alpaca: chart reads for {len(out)}/{len(tickers)} tickers")
    return out
