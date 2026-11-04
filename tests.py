"""Offline tests on sample data (live endpoints are not reachable from the build sandbox).
Run: python tests.py"""
from datetime import datetime, timedelta, timezone

import sources_news
from common import Co, find_by_name, name_index, norm
from scanner import build_alert, direction, score
from sources_awards import award_pts, parse_dod
from sources_news import classify, edgar_signals, extract_tickers
from sources_volume import ET, session_fraction, volume_signals
from tape import Book, surge

UNI = {
    "ZPHC": Co("ZPHC", "Zenith Photonics Corp.", 1234567, 80_000_000, 2.50, 900_000, 1.5),
    "QBLT": Co("QBLT", "Quantum Bolt Industries, Inc.", 7654321, 20_000_000, 0.80, 300_000, -2.0),
    "AMRC": Co("AMRC", "American Technologies Inc", 1111111, 50_000_000, 1.0, 100_000, 0.0),
}
IDX = name_index(UNI)

# --- name matching
assert norm("Zenith Photonics Corp.") == "zenith photonics"
assert IDX["zenith photonics"] == "ZPHC"

# --- DoD parsing + matching
dod = (
    "Zenith Photonics Corp., Austin, Texas, has been awarded a $9,998,520 firm-fixed-price contract for "
    "laser components. Bids were solicited via the internet with one received. Work will be performed in Austin.\n"
    "Other Defense Contractor LLC, Reston, Virginia, was awarded a $12,000,000 contract for widgets and support services.\n"
    "short line"
)
rows = parse_dod(dod)
assert len(rows) == 2 and rows[0][1] == 9_998_520, rows
assert find_by_name(rows[0][0], IDX) == [("ZPHC", "exact")]
assert find_by_name(rows[1][0], IDX) == []
assert find_by_name("Technologies Group, Boston, MA", IDX) == []  # generic words must not match
assert award_pts(9_998_520, 80_000_000) == 4.0  # 12.5% of market cap
assert award_pts(100_000, 80_000_000) == 0.0    # immaterial

# --- press-release classification + ticker extraction
assert extract_tickers("Zenith Photonics (Nasdaq: ZPHC) today announced") == ["ZPHC"]
assert extract_tickers("shares of Foo (OTCQB: FOOO) rose") == ["FOOO"]
pts, tags = classify("Zenith (Nasdaq: ZPHC) receives FDA approval for its device")
assert pts >= 3 and "FDA/regulatory win" in tags, (pts, tags)
pts, tags = classify("Quantum Bolt announces pricing of $10 million registered direct offering")
assert pts <= -3 and "DILUTION" in tags, (pts, tags)
pts, tags = classify("Company announces $5M contract award from the U.S. Army and pricing of public offering")
assert -3 < pts < 4, (pts, tags)  # bullish and bearish partially offset
assert classify("Company to host quarterly earnings call") == (0.0, [])

# --- 8-K atom parsing
ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>
<title>8-K - Zenith Photonics Corp. (0001234567) (Filer)</title>
<link href="https://www.sec.gov/Archives/edgar/data/1234567/x-index.htm"/>
<summary type="html">&lt;b&gt;Filed:&lt;/b&gt; 2026-11-03 &lt;b&gt;AccNo:&lt;/b&gt; 0001-26-1 Item 1.01: Entry into a Material Definitive Agreement Item 9.01: Exhibits</summary>
<id>urn:tag:sec.gov,2008:accession-number=0001234567-26-000001</id></entry>
<entry><title>8-K - Quantum Bolt Industries, Inc. (0007654321) (Filer)</title>
<link href="https://www.sec.gov/Archives/edgar/data/7654321/y-index.htm"/>
<summary type="html">Item 3.02: Unregistered Sales of Equity Securities Item 9.01</summary>
<id>urn:tag:sec.gov,2008:accession-number=0007654321-26-000002</id></entry></feed>"""


class FakeResp:
    content = ATOM


sources_news.get = lambda *a, **k: FakeResp()
sigs = edgar_signals(UNI)
by = {s.ticker: s for s in sigs}
assert by["ZPHC"].pts == 1.0 and by["QBLT"].pts == -2.0, sigs

# --- volume signals
tue_11 = datetime(2026, 11, 3, 11, 0, tzinfo=ET)
assert session_fraction(datetime(2026, 11, 7, 11, 0, tzinfo=ET)) is None  # Saturday
assert session_fraction(datetime(2026, 11, 3, 9, 35, tzinfo=ET)) is None  # too early in the open
frac = session_fraction(tue_11)
assert 0.3 < frac < 0.7, frac
vs = volume_signals(UNI, {"ZPHC": 100_000, "QBLT": 250_000, "AMRC": 90_000}, tue_11)
tick = {s.ticker for s in vs}
assert "ZPHC" in tick and "QBLT" not in tick  # QBLT volume is normal
assert sum(s.pts for s in vs if s.ticker == "ZPHC") >= 3
assert all(s.pts > 0 for s in vs if s.ticker == "ZPHC")  # price up -> bullish
assert not [s for s in vs if s.ticker == "AMRC" and s.pts < 0]

# --- scoring, decay, direction, alert text
now = datetime.now(timezone.utc)
iso = lambda h: (now - timedelta(hours=h)).isoformat()  # noqa: E731
S = [
    {"ticker": "ZPHC", "kind": "DOD", "key": "a", "pts": 4.0, "text": "DoD contract", "url": "u", "ts": iso(1)},
    {"ticker": "ZPHC", "kind": "VOL", "key": "b", "pts": 3.0, "text": "RVOL 8x", "url": "", "ts": iso(50)},  # decays to 30%
]
total, pos, neg = score(S, now)
assert abs(total - (4 + 0.9)) < 1e-6 and neg == 0
assert direction(total, pos, neg) == "BULLISH"
assert direction(-4, 1, -5) == "BEARISH" and direction(1, 6, -5) == "MIXED"
title, short, long = build_alert("ZPHC", UNI["ZPHC"], S, total, pos, neg, {"a"})
assert title.startswith("BULLISH ZPHC") and "*DOD" in long and len(short) < 400

# --- tape: buy/sell classification + surge detection
b = Book()
b.quote(1.00, 1.02)
t0 = 1_000_000.0
b.trade(1.02, 3000, t0)       # at ask -> buy
b.trade(1.00, 500, t0 + 1)    # at bid -> sell
b.trade(1.012, 100, t0 + 2)   # above mid (1.01) -> buy
b.trade(1.008, 100, t0 + 3)   # below mid -> sell
w = b.window(60, t0 + 4)
assert (w["buy"], w["sell"]) == (3100, 600), w
b2 = Book()
b2.trade(1.00, 10, t0)
b2.trade(1.01, 10, t0 + 1)    # tick up -> buy
b2.trade(1.00, 10, t0 + 2)    # tick down -> sell
assert [t[1] for t in b2.trades] == [1, 1, -1] or [t[1] for t in b2.trades][1:] == [1, -1]
big = Book()
big.quote(2.00, 2.02)
for i in range(30):
    big.trade(2.02, 400, t0 + i)  # 12,000 shares all buys in 30s
assert surge(big.stats(t0 + 31)) == "BUY"
quiet = Book()
quiet.quote(2.00, 2.02)
quiet.trade(2.02, 400, t0)
assert surge(quiet.stats(t0 + 5)) is None
mixed = Book()
mixed.quote(2.00, 2.02)
for i in range(30):
    mixed.trade(2.02 if i % 2 else 2.00, 400, t0 + i)  # heavy but balanced
assert surge(mixed.stats(t0 + 31)) is None

from sources_alpaca import parse_snapshot
_p, _v, _pct = parse_snapshot({"latestTrade": {"p": 1.10}, "dailyBar": {"v": 5000, "c": 1.09}, "prevDailyBar": {"c": 1.00}})
assert (_p, _v) == (1.10, 5000) and abs(_pct - 10.0) < 1e-9, (_p, _v, _pct)
assert parse_snapshot({}) == (None, None, None)

print("all offline tests passed")
