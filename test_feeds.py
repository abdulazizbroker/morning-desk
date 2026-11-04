"""Offline tests for the feed parser and the press-release pipeline. Run: python test_feeds.py"""
import time
from email.utils import formatdate

import sources_news
from common import Co, name_index
from feedparse import parse_feed

now_rfc = formatdate(time.time(), usegmt=True)
old_rfc = formatdate(time.time() - 80 * 3600, usegmt=True)
RSS = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
<item><title>Zenith Photonics Awarded $4M Army Contract</title><link>https://ex.com/a</link>
<description>&lt;p&gt;AUSTIN (Nasdaq: ZPHC) today announced it was awarded a $4 million contract by the U.S. Army.&lt;/p&gt;</description>
<pubDate>{now_rfc}</pubDate><guid>a</guid></item>
<item><title>Quantum Bolt Prices $8M Registered Direct Offering</title><link>https://ex.com/b</link>
<description>Quantum Bolt (NASDAQ: QBLT) announced pricing of a registered direct offering.</description>
<pubDate>{now_rfc}</pubDate></item>
<item><title>Zenith Photonics Announces Old News Contract Award</title><link>https://ex.com/c</link>
<description>(Nasdaq: ZPHC) awarded</description><pubDate>{old_rfc}</pubDate></item>
<item><title>Big Corp (NYSE: BIGC) wins contract</title><link>https://ex.com/d</link>
<description>awarded a contract</description><pubDate>{now_rfc}</pubDate></item>
</channel></rss>""".encode()

items = parse_feed(RSS)
assert len(items) == 4 and items[0]["link"] == "https://ex.com/a" and items[0]["ts"] > time.time() - 60

UNI = {
    "ZPHC": Co("ZPHC", "Zenith Photonics Corp.", 1, 80_000_000, 2.5, 900_000, 1.5),
    "QBLT": Co("QBLT", "Quantum Bolt Industries, Inc.", 2, 20_000_000, 0.8, 300_000, -2.0),
}


class FakeResp:
    content = RSS


sources_news.get = lambda *a, **k: FakeResp()
sigs = sources_news.pr_signals(UNI, name_index(UNI))
by = {s.ticker: s for s in sigs}
assert set(by) == {"ZPHC", "QBLT"}, by          # BIGC not in universe; old item skipped
assert by["ZPHC"].pts >= 3 and by["QBLT"].pts <= -3
assert len([s for s in sigs if s.ticker == "ZPHC"]) == 1  # 7 feeds returned the same links: deduped
print("feed tests passed")
