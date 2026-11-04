"""Unusual-volume detection (delayed, scanner grade). For true real-time see tape.py."""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from common import MIN_AVG_VOL, MIN_DOLLAR_VOL, Signal, log, money

ET = ZoneInfo("America/New_York")


def session_fraction(now_et: datetime) -> float | None:
    """Rough share of a normal day's volume expected by now (front-loaded curve).
    None = outside the window where the screener volume is meaningful."""
    if now_et.weekday() >= 5:
        return None
    m = (now_et.hour * 60 + now_et.minute) - (9 * 60 + 30)
    if m < 15:
        return None
    if m >= 390:
        return 1.0 if m < 390 + 240 else None
    return max((m / 390) ** 0.6, 0.05)


def refresh_avgvol(tickers: list[str]) -> dict[str, float]:
    """20-day average daily volume via Yahoo, in chunks. Run once a day and cached."""
    import yfinance as yf

    today = datetime.now(ET).date()
    out: dict[str, float] = {}
    for i in range(0, len(tickers), 150):
        chunk = tickers[i : i + 150]
        try:
            df = yf.download(chunk, period="2mo", interval="1d", group_by="ticker",
                             auto_adjust=False, progress=False, threads=True)
        except Exception as e:  # noqa: BLE001
            log("yahoo chunk failed:", e)
            time.sleep(3)
            continue
        for t in chunk:
            try:
                v = (df[t]["Volume"] if t in df.columns.get_level_values(0) else df["Volume"]).dropna()
                v = v[[d.date() < today for d in v.index]]
                if len(v) >= 10:
                    out[t] = float(v.tail(20).mean())
            except Exception:  # noqa: BLE001
                continue
        time.sleep(1.0)
    return out


def volume_signals(uni, avgvol: dict[str, float], now_et: datetime) -> list[Signal]:
    frac = session_fraction(now_et)
    if frac is None:
        return []
    day = now_et.date().isoformat()
    out: list[Signal] = []
    for t, co in uni.items():
        avg = avgvol.get(t)
        if not avg or avg < MIN_AVG_VOL or not co.volume or not co.price:
            continue
        if co.volume * co.price < MIN_DOLLAR_VOL:
            continue
        rvol = co.volume / (avg * frac)
        tier = 3 if rvol >= 10 else 2 if rvol >= 5 else 1 if rvol >= 3 else 0
        if not tier:
            continue
        pct = co.pct or 0.0
        stealth = abs(pct) < 4 and rvol >= 5
        move = f"{pct:+.1f}%"
        label = f"RVOL {rvol:.1f}x ({co.volume:,.0f} sh, {money(co.volume * co.price)}) price {move}"
        if stealth:
            label += " | STEALTH: heavy volume, price barely moved"
        # volume is directional only via price: up on volume leans bullish, down leans bearish
        sign = 1 if pct >= 0 else -1
        for k in range(1, tier + 1):  # incremental so tiers add up to 1, 2, 3
            out.append(Signal(
                t, "VOL", f"VOL:{t}:{day}:{k}:{'u' if sign > 0 else 'd'}", float(sign),
                label if k == tier else label + " (earlier tier)",
                f"https://finance.yahoo.com/quote/{t}",
            ))
        if stealth:
            out.append(Signal(t, "VOL", f"VOL:{t}:{day}:stealth", 1.0, "stealth volume bonus"))
    return out
