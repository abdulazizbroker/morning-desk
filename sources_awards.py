"""Federal money: USAspending awards/grants + the daily DoD (War Dept) contract list."""
from __future__ import annotations

import difflib
import hashlib
import html
import re
import time
from datetime import date, timedelta

import requests

from common import LOOKBACK_DAYS, MIN_AWARD, Signal, find_by_name, get, log, money, norm
from feedparse import parse_feed

USA_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
GROUPS = {"Contract": ["A", "B", "C", "D"], "Grant": ["02", "03", "04", "05"]}
MAX_PAGES = 30  # x100 rows, sorted by amount desc, so the most material awards come first
DOD_FEED = "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=400&Site=945&max=10"


def award_pts(amount: float, mcap: float | None) -> float:
    """Bullishness of an award, scaled by how big it is relative to the company."""
    if not mcap:
        return 1.5
    pct = amount / mcap
    if pct >= 0.25:
        return 5.0
    if pct >= 0.10:
        return 4.0
    if pct >= 0.03:
        return 3.0
    if pct >= 0.01:
        return 1.5
    return 0.0


def usaspending_signals(uni, idx) -> list[Signal]:
    by_prefix: dict[str, list[str]] = {}
    for k in idx:
        by_prefix.setdefault(k[:3], []).append(k)
    end = date.today()
    start = end - timedelta(days=LOOKBACK_DAYS)
    out: list[Signal] = []
    for kind, codes in GROUPS.items():
        for page in range(1, MAX_PAGES + 1):
            body = {
                "filters": {
                    "award_type_codes": codes,
                    "time_period": [{"start_date": start.isoformat(), "end_date": end.isoformat(), "date_type": "action_date"}],
                    "award_amounts": [{"lower_bound": MIN_AWARD}],
                },
                "fields": ["Award ID", "Recipient Name", "Award Amount", "Awarding Agency", "Start Date", "Description"],
                "sort": "Award Amount", "order": "desc", "limit": 100, "page": page,
            }
            r = requests.post(USA_URL, json=body, timeout=60)
            r.raise_for_status()
            data = r.json()
            for row in data.get("results", []):
                n = norm(row.get("Recipient Name") or "")
                if not n:
                    continue
                t, how = idx.get(n), "exact"
                if not t:
                    close = difflib.get_close_matches(n, by_prefix.get(n[:3], []), n=1, cutoff=0.93)
                    t, how = (idx[close[0]], "fuzzy") if close else (None, None)
                if not t or t not in uni:
                    continue
                amt = float(row.get("Award Amount") or 0)
                co = uni[t]
                pts = award_pts(amt, co.mcap)
                if pts <= 0:
                    continue
                if how != "exact":
                    pts *= 0.7
                pct = f"{amt / co.mcap:.1%} of mcap" if co.mcap else "mcap n/a"
                aid = row.get("Award ID")
                out.append(Signal(
                    t, "AWARD", f"AWD:{kind}:{aid}", round(pts, 1),
                    f"{kind} {money(amt)} ({pct}) from {row.get('Awarding Agency')} [{how} match: {row.get('Recipient Name')}]",
                    f"https://www.usaspending.gov/keyword_search/{aid}",
                ))
            if not data.get("page_metadata", {}).get("hasNext"):
                break
            time.sleep(0.3)
    return out


# ---------------- DoD / War Department daily contracts ----------------
_AWARDED = re.compile(
    r"\b(?:has|have|is|are|was|were)\s+(?:been\s+|being\s+)?(?:awarded|selected|issued|modified)\b|\bawarded\b", re.I
)
_AMT = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s*(million|billion)?", re.I)


def html_to_text(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h\d>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s)


def parse_dod(text: str) -> list[tuple[str, float | None, str]]:
    """-> [(head_before_'awarded', dollar_amount, full_paragraph)]"""
    out = []
    for para in (p.strip() for p in text.split("\n")):
        if len(para) < 80:
            continue
        m = _AWARDED.search(para)
        head = para[: m.start()] if m else para[:200]
        a = _AMT.search(para)
        amt = None
        if a:
            mult = {"million": 1e6, "billion": 1e9}.get((a.group(2) or "").lower(), 1)
            amt = float(a.group(1).replace(",", "")) * mult
        out.append((head[:300], amt, para))
    return out


def dod_signals(uni, idx) -> list[Signal]:
    r = get(DOD_FEED)
    out: list[Signal] = []
    for e in parse_feed(r.content):
        text = html_to_text(e.get("summary") or e.get("description") or "")
        if len(text) < 400 and e.get("link"):
            try:
                text = html_to_text(get(e["link"]).text)
            except Exception as ex:  # noqa: BLE001
                log("dod page fetch failed:", ex)
        for head, amt, para in parse_dod(text):
            for t, how in find_by_name(head, idx):
                co = uni.get(t)
                if not co:
                    continue
                pts = award_pts(amt, co.mcap) if amt else 2.0
                if how != "exact":
                    pts *= 0.6
                if pts <= 0:
                    continue
                pct = f" ({amt / co.mcap:.1%} of mcap)" if amt and co.mcap else ""
                sole = " SOLE-SOURCE" if re.search(r"sole[- ]source|single[- ]source", para, re.I) else ""
                out.append(Signal(
                    t, "DOD", f"DOD:{hashlib.md5(para.encode()).hexdigest()[:12]}:{t}", round(pts, 1),
                    f"DoD contract {money(amt) if amt else 'amt n/a'}{pct}{sole} [{how} match] {para[:160]}",
                    e.get("link", ""),
                ))
    return out
