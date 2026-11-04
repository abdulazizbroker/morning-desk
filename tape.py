"""Real-time tape reader: live buy vs sell volume per symbol, local dashboard, surge alerts.

Run this on an always-on machine (your PC, a Raspberry Pi, a cheap VPS). It cannot run on
GitHub Actions.

Data: Alpaca websocket. FREE plan = real-time but IEX-only (a slice of total volume) and
30 symbols max. The $99/mo Algo Trader Plus plan = all US exchanges, unlimited symbols
(set ALPACA_FEED=sip). Check Alpaca's current plan page before paying.

"Buy" vs "sell" is INFERRED (no feed labels it): trades at/above the ask or above the
midpoint count as buys, at/below the bid or below the midpoint as sells, ties use the
tick rule. Treat it as pressure, not truth.

  export ALPACA_KEY=... ALPACA_SECRET=...
  python tape.py ABCD EFGH IJKL          # or tape_symbols.txt / TAPE_SYMBOLS / state/hot.json
  open http://localhost:8765
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from common import load_json, log, money, notify

MIN_BURST = float(os.getenv("TAPE_MIN_BURST_SHARES", 5000))  # shares in 60s to count as a burst
IMBALANCE = float(os.getenv("TAPE_IMBALANCE", 0.7))          # |buy-sell|/(buy+sell) needed
BURST_X = float(os.getenv("TAPE_BURST_X", 4))                # x the symbol's own per-minute baseline
COOLDOWN_S = float(os.getenv("TAPE_COOLDOWN_S", 300))
LOCK = threading.Lock()


class Book:
    def __init__(self):
        self.trades: deque = deque()  # (t, side, size, price)
        self.bid = self.ask = self.last = None
        self.side = 1
        self.minutes: deque = deque(maxlen=30)  # completed per-minute share volumes
        self.cur_min = int(time.time() // 60)
        self.cur_vol = 0.0

    def quote(self, bid, ask):
        if bid and ask and bid > 0 and ask >= bid:
            self.bid, self.ask = bid, ask

    def classify(self, price: float) -> int:
        if self.bid and self.ask:
            if price >= self.ask:
                return 1
            if price <= self.bid:
                return -1
            mid = (self.bid + self.ask) / 2
            if price > mid:
                return 1
            if price < mid:
                return -1
        if self.last is not None:  # tick rule
            if price > self.last:
                return 1
            if price < self.last:
                return -1
        return self.side

    def trade(self, price: float, size: float, now: float | None = None):
        now = now or time.time()
        side = self.classify(price)
        self.side, self.last = side, price
        self.trades.append((now, side, size, price))
        while self.trades and self.trades[0][0] < now - 300:
            self.trades.popleft()
        m = int(now // 60)
        if m != self.cur_min:
            self.minutes.append(self.cur_vol)
            self.cur_min, self.cur_vol = m, 0.0
        self.cur_vol += size

    def window(self, secs: float, now: float):
        buy = sell = 0.0
        n = 0
        for t, side, size, _p in reversed(self.trades):
            if t < now - secs:
                break
            n += 1
            if side > 0:
                buy += size
            else:
                sell += size
        tot = buy + sell
        return {"buy": buy, "sell": sell, "imb": (buy - sell) / tot if tot else 0.0, "n": n}

    def baseline(self) -> float | None:
        return sum(self.minutes) / len(self.minutes) if len(self.minutes) >= 5 else None

    def stats(self, now: float | None = None) -> dict:
        now = now or time.time()
        w10, w60, w300 = self.window(10, now), self.window(60, now), self.window(300, now)
        return {"last": self.last, "w10": w10, "w60": w60, "w300": w300,
                "tpm": w60["n"], "base": self.baseline()}


def surge(stats: dict) -> str | None:
    w = stats["w60"]
    vol = w["buy"] + w["sell"]
    need = max(MIN_BURST, BURST_X * (stats["base"] or 0))
    if vol >= need and abs(w["imb"]) >= IMBALANCE:
        return "BUY" if w["imb"] > 0 else "SELL"
    return None


BOOKS: dict[str, Book] = defaultdict(Book)
SNAP: list = []
LAST_ALERT: dict[tuple[str, str], float] = {}


def loop(symbols):
    global SNAP
    while True:
        time.sleep(1)
        now = time.time()
        rows = []
        with LOCK:
            for s in symbols:
                st = BOOKS[s].stats(now)
                rows.append({"sym": s, **st})
        SNAP = rows
        for r in rows:
            side = surge(r)
            if side and now - LAST_ALERT.get((r["sym"], side), 0) > COOLDOWN_S:
                LAST_ALERT[(r["sym"], side)] = now
                w = r["w60"]
                px = r["last"] or 0
                title = f"{side} SURGE {r['sym']}"
                body = (f"{title}: {w['buy']:,.0f} buy vs {w['sell']:,.0f} sell shares in 60s "
                        f"({w['imb']:+.0%} imbalance) at ${px:.2f}, {money((w['buy'] + w['sell']) * px)} traded. "
                        f"Inferred from tape; verify on your chart.")
                notify(float(os.getenv("TAPE_ALERT_SCORE", 3)), title, body, body)


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">
<title>Live tape</title>
<style>body{background:#0b0e14;color:#d6deeb;font:14px system-ui;margin:16px}
table{border-collapse:collapse;width:100%}td,th{padding:6px 10px;text-align:right;border-bottom:1px solid #1f2430}
th:first-child,td:first-child{text-align:left;font-weight:600}.bar{height:10px;background:#c0392b;min-width:140px}
.bar i{display:block;height:100%;background:#27ae60}.up{color:#2ecc71}.dn{color:#e74c3c}.hot{background:#1b2a1b}.cold{background:#2a1b1b}
small{color:#7f8aa3}</style>
<h3>Live tape <small id=s>connecting...</small></h3>
<table><thead><tr><th>Sym<th>Last<th>Buy/Sell 10s<th>Buy/Sell 60s<th>Pressure 60s<th>Imb 60s<th>Trades/min</thead><tbody id=b></tbody></table>
<small>Buy/sell inferred from trades vs quotes. IEX-only on the free feed, so volumes are a sample.</small>
<script>
const f=n=>n>=1e6?(n/1e6).toFixed(2)+'M':n>=1e3?(n/1e3).toFixed(1)+'k':Math.round(n);
async function tick(){try{const r=await (await fetch('/data')).json();
r.sort((a,b)=>(b.w60.buy+b.w60.sell)-(a.w60.buy+a.w60.sell));
document.getElementById('b').innerHTML=r.map(x=>{const t=x.w60.buy+x.w60.sell,p=t?100*x.w60.buy/t:50,i=x.w60.imb;
return `<tr class="${i>.7&&t>0?'hot':i<-.7?'cold':''}"><td>${x.sym}<td>${x.last?x.last.toFixed(x.last<1?4:2):'-'}
<td><span class=up>${f(x.w10.buy)}</span> / <span class=dn>${f(x.w10.sell)}</span>
<td><span class=up>${f(x.w60.buy)}</span> / <span class=dn>${f(x.w60.sell)}</span>
<td><div class=bar><i style="width:${p}%"></i></div><td class="${i>=0?'up':'dn'}">${(100*i).toFixed(0)}%<td>${x.tpm}`}).join('');
document.getElementById('s').textContent='updated '+new Date().toLocaleTimeString()}catch(e){document.getElementById('s').textContent='disconnected'}}
setInterval(tick,1000);tick();</script>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/data"):
            body, ctype = json.dumps(SNAP).encode(), "application/json"
        else:
            body, ctype = PAGE.encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence request logging
        pass


def pick_symbols(cli: list[str]) -> list[str]:
    if cli:
        syms = cli
    elif os.getenv("TAPE_SYMBOLS"):
        syms = os.getenv("TAPE_SYMBOLS").split(",")
    elif os.path.exists("tape_symbols.txt"):
        syms = [x.strip() for x in open("tape_symbols.txt") if x.strip() and not x.startswith("#")]
    else:  # hottest names from the scanner, if it has run in this folder
        syms = [h["ticker"] for h in load_json("hot.json", [])]
    return [s.upper() for s in syms][: 30 if os.getenv("ALPACA_FEED", "iex").lower() != "sip" else 200]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    key, secret = os.getenv("ALPACA_KEY"), os.getenv("ALPACA_SECRET")
    if not (key and secret):
        raise SystemExit("Set ALPACA_KEY and ALPACA_SECRET (free account at alpaca.markets)")
    symbols = pick_symbols(a.symbols)
    if not symbols:
        raise SystemExit("No symbols: pass them as arguments, or fill tape_symbols.txt")

    from alpaca.data.enums import DataFeed
    from alpaca.data.live import StockDataStream

    feed = DataFeed.SIP if os.getenv("ALPACA_FEED", "iex").lower() == "sip" else DataFeed.IEX
    stream = StockDataStream(key, secret, feed=feed)

    async def on_trade(t):
        with LOCK:
            BOOKS[t.symbol].trade(float(t.price), float(t.size))

    async def on_quote(q):
        with LOCK:
            BOOKS[q.symbol].quote(float(q.bid_price), float(q.ask_price))

    stream.subscribe_trades(on_trade, *symbols)
    stream.subscribe_quotes(on_quote, *symbols)
    threading.Thread(target=loop, args=(symbols,), daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"tracking {len(symbols)} symbols on {feed.name}; dashboard http://localhost:{a.port}")
    stream.run()


if __name__ == "__main__":
    main()
