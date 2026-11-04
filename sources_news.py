"""Press-release wires + SEC 8-K feed, classified bullish/bearish."""
from __future__ import annotations

import hashlib
import re
import time

from common import Signal, find_by_name, get, log
from feedparse import parse_feed
from sources_awards import html_to_text

DEFAULT_FEEDS = [
    "https://www.prnewswire.com/rss/news-releases-list.rss",
    "https://www.prnewswire.com/rss/health-latest-news/health-latest-news-list.rss",
    "https://www.prnewswire.com/rss/general-business-latest-news/general-business-latest-news-list.rss",
    "https://www.prnewswire.com/rss/business-technology-latest-news/business-technology-latest-news-list.rss",
    "https://www.prnewswire.com/rss/financial-services-latest-news/financial-services-latest-news-list.rss",
    "https://www.prnewswire.com/rss/automotive-transportation-latest-news/automotive-transportation-latest-news-list.rss",
    "https://www.prnewswire.com/rss/consumer-products-retail-latest-news/consumer-products-retail-latest-news-list.rss",
]
EDGAR_8K = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K"
    "&company=&dateb=&owner=include&start=0&count=100&output=atom"
)

BULL = [
    (r"fda (?:approv|clear|grant)|510\(k\)|pma approval|marketing authori[sz]ation", 3, "FDA/regulatory win"),
    (r"met (?:its |the )?primary endpoint|positive (?:topline|top-line|phase|interim)|statistically significant", 3, "positive trial data"),
    (r"\bawarded\b|contract award|purchase order|task order|\bidiq\b|wins? [^.]{0,30}contract", 3, "contract/award"),
    (r"to be acquired|definitive (?:merger )?agreement|acquired by", 3, "M&A"),
    (r"(?:receives?|awarded|secures?|announces?)[^.]{0,60}\bgrant\b|\bgrant (?:award|funding)\b|\bsbir\b|\bsttr\b", 2, "grant"),
    (r"darpa|barda|\bnasa\b|department of (?:defense|war|energy)|u\.s\. (?:army|navy|air force)|\bnih\b", 2, "gov customer"),
    (r"collaboration|partnership|license agreement|letter of intent|strategic agreement|distribution agreement", 2, "deal/partnership"),
    (r"fast track|breakthrough therapy|orphan drug|rare pediatric", 2, "regulatory designation"),
    (r"patent (?:granted|issued)|uplist", 1, "patent/uplist"),
]
BEAR = [
    (r"public offering|registered direct|private placement|at[- ]the[- ]market|warrant inducement|prices? [^.]{0,40}offering|pricing of", -3, "DILUTION"),
    (r"reverse (?:stock )?split|going concern|delist|minimum bid|deficienc(?:y|ies)|bankruptcy|chapter 11", -3, "DISTRESS"),
    (r"complete response letter|clinical hold|fails? to meet|did not meet|discontinu", -3, "negative biotech/ops"),
]
TICKER_RE = re.compile(
    r"(?:NASDAQ|NYSE(?: American| MKT| Arca)?|AMEX|OTC[A-Z]*|OTCQB|OTCQX)\s*[:\-]\s*\$?([A-Z]{1,5}(?:\.[A-Z])?)\b", re.I
)


def classify(text: str) -> tuple[float, list[str]]:
    t = text.lower()
    bulls = sorted(((p, tag) for rx, p, tag in BULL if re.search(rx, t)), reverse=True)
    bears = sorted((p, tag) for rx, p, tag in BEAR if re.search(rx, t))
    pts, tags = 0.0, []
    if bulls:
        pts += bulls[0][0] + (1 if len(bulls) > 1 else 0)
        tags += [b[1] for b in bulls[:2]]
    if bears:
        pts += bears[0][0]
        tags += [b[1] for b in bears[:2]]
    return max(min(pts, 4.0), -5.0) if pts else 0.0, tags


def extract_tickers(text: str) -> list[str]:
    out = []
    for m in TICKER_RE.finditer(text):
        sym = m.group(1).upper().replace(".", "-")
        if sym not in out:
            out.append(sym)
    return out


def load_feeds() -> list[str]:
    try:
        with open("feeds.txt") as f:
            urls = [x.strip() for x in f if x.strip() and not x.startswith("#")]
            return urls or DEFAULT_FEEDS
    except FileNotFoundError:
        return DEFAULT_FEEDS


def _age_hours(entry) -> float | None:
    ts = entry.get("ts")
    return (time.time() - ts) / 3600 if ts else None


def pr_signals(uni, idx) -> list[Signal]:
    out: list[Signal] = []
    seen_links = set()
    for url in load_feeds():
        try:
            entries = parse_feed(get(url).content)
        except Exception as e:  # noqa: BLE001
            log("feed failed:", url, e)
            continue
        for e in entries:
            link = e.get("link", "")
            if link in seen_links:
                continue
            seen_links.add(link)
            age = _age_hours(e)
            if age is not None and age > 36:
                continue
            title = e.get("title", "")
            text = f"{title}. {html_to_text(e.get('summary') or e.get('description') or '')}"
            tickers = [t for t in extract_tickers(text) if t in uni]
            if not tickers:
                tickers = [t for t, how in find_by_name(title, idx) if how == "exact"]
            if not tickers:
                continue
            pts, tags = classify(text)
            if pts == 0:
                continue
            for t in tickers:
                out.append(Signal(
                    t, "PR", f"PR:{hashlib.md5(link.encode()).hexdigest()[:12]}:{t}", pts,
                    f"PR [{', '.join(tags)}]: {title[:120]}", link,
                ))
    return out


ITEM_PTS = {
    "1.01": (1.0, "material agreement (check: deal or financing?)"),
    "2.01": (1.0, "acquisition/disposition completed"),
    "7.01": (0.5, "Reg FD disclosure"),
    "8.01": (0.5, "other events"),
    "3.02": (-2.0, "unregistered equity sale (dilution)"),
    "3.01": (-3.0, "delisting/listing notice"),
    "1.03": (-5.0, "bankruptcy"),
    "4.02": (-4.0, "non-reliance on past financials"),
    "2.04": (-2.0, "debt acceleration"),
}


def edgar_signals(uni) -> list[Signal]:
    cik_map = {co.cik: co.ticker for co in uni.values() if co.cik}
    out: list[Signal] = []
    for e in parse_feed(get(EDGAR_8K, sec=True).content):
        m = re.search(r"\((\d{10})\)", e.get("title", ""))
        if not m:
            continue
        t = cik_map.get(int(m.group(1)))
        if not t:
            continue
        items = re.findall(r"Item\s+(\d\.\d{2})", html_to_text(e.get("summary", "")))
        hits = [ITEM_PTS[i] for i in items if i in ITEM_PTS]
        if not hits:
            continue
        pos = max([h[0] for h in hits if h[0] > 0], default=0)
        neg = min([h[0] for h in hits if h[0] < 0], default=0)
        pts = pos + neg
        if pts == 0:
            continue
        out.append(Signal(
            t, "8K", f"8K:{e.get('id') or e.get('link')}", pts,
            f"8-K items {', '.join(sorted(set(items)))}: " + "; ".join(h[1] for h in hits[:2]),
            e.get("link", ""),
        ))
    return out
