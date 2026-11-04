"""Shared config, HTTP, state, notifications, and the micro-cap universe."""
from __future__ import annotations

import difflib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")  # Toronto = New York time, so your 6am-2pm = 6:00-14:00 ET


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


# ---- tunables (all overridable via env vars) ----
MAX_MCAP = _f("MAX_MCAP", 750_000_000)
MIN_MCAP = _f("MIN_MCAP", 3_000_000)
MIN_PRICE = _f("MIN_PRICE", 0.10)
MIN_AVG_VOL = _f("MIN_AVG_VOL", 30_000)
MIN_DOLLAR_VOL = _f("MIN_DOLLAR_VOL", 100_000)
MIN_AWARD = _f("MIN_AWARD", 250_000)
LOOKBACK_DAYS = int(_f("LOOKBACK_DAYS", 7))


def _window(s: str) -> tuple[int, int]:
    a, b = s.split("-")
    h1, m1 = a.split(":")
    h2, m2 = b.split(":")
    return int(h1) * 60 + int(m1), int(h2) * 60 + int(m2)


# Alerts reach your phone only inside this window (ET, weekdays). Outside it they are queued
# and delivered in the next morning game plan. Anything >= URGENT_MIN still pushes (no SMS).
WORK_WINDOW = _window(os.getenv("WORK_WINDOW_ET", "05:50-14:15"))
URGENT_MIN = _f("URGENT_MIN_SCORE", 8)


def in_work_window(now_et: datetime) -> bool:
    m = now_et.hour * 60 + now_et.minute
    return now_et.weekday() < 5 and WORK_WINDOW[0] <= m <= WORK_WINDOW[1]
SMS_MIN = _f("SMS_MIN_SCORE", 5)    # texts only for strong confluence
PUSH_MIN = _f("PUSH_MIN_SCORE", 3)  # Telegram / ntfy for everything decent
STATE_DIR = os.getenv("STATE_DIR", "state")
SEC_UA = os.getenv("SEC_USER_AGENT") or "micro-cap-scanner admin@example.com"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

TW_SID = os.getenv("TWILIO_ACCOUNT_SID")
TW_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TW_FROM = os.getenv("TWILIO_FROM")
SMS_TO = os.getenv("SMS_TO")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")
NTFY_TOPIC = os.getenv("NTFY_TOPIC")  # free phone push: install the ntfy app, pick a secret topic


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def money(x: float) -> str:
    if x >= 1e9:
        return f"${x / 1e9:.2f}B"
    if x >= 1e6:
        return f"${x / 1e6:.1f}M"
    return f"${x:,.0f}"


# ---- HTTP ----
def get(url, *, headers=None, params=None, timeout=30, retries=3, sec=False):
    h = {"User-Agent": SEC_UA if sec else BROWSER_UA, "Accept": "*/*"}
    h.update(headers or {})
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=h, params=params, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                last = RuntimeError(f"HTTP {r.status_code} for {url}")
                time.sleep(2 ** i)
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(2 ** i)
    raise last or RuntimeError(f"failed: {url}")


# ---- state ----
def _path(name: str) -> str:
    os.makedirs(STATE_DIR, exist_ok=True)
    return os.path.join(STATE_DIR, name)


def state_exists(name: str) -> bool:
    return os.path.exists(os.path.join(STATE_DIR, name))


def load_json(name: str, default):
    try:
        with open(_path(name)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(name: str, obj) -> None:
    with open(_path(name), "w") as f:
        json.dump(obj, f)


# ---- signals ----
@dataclass
class Signal:
    ticker: str
    kind: str      # AWARD | DOD | PR | 8K | VOL
    key: str       # unique id, used for de-duplication
    pts: float
    text: str
    url: str = ""
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ---- name matching ----
SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "llc", "ltd",
    "limited", "plc", "lp", "llp", "holdings", "holding", "group", "the", "sa", "nv",
    "de", "cv", "ag",
}
STOP_SINGLE = {
    "american", "global", "united", "national", "advanced", "technologies", "technology",
    "systems", "solutions", "general", "applied", "integrated", "international", "digital",
    "energy", "capital", "financial", "resources", "industries", "services", "partners",
    "pharmaceuticals", "therapeutics", "biosciences", "sciences", "networks", "medical",
}


def norm(name: str) -> str:
    name = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    return " ".join(p for p in name.split() if p not in SUFFIXES)


def find_by_name(text: str, idx: dict[str, str]) -> list[tuple[str, str]]:
    """Companies whose normalized name appears inside free text.
    Returns [(ticker, 'exact'|'partial')]. Partial = first two words only."""
    h = " " + norm(text) + " "
    hits: dict[str, str] = {}
    for k, t in idx.items():
        if len(k) < 6:
            continue
        toks = k.split()
        if len(toks) == 1 and (len(k) < 7 or k in STOP_SINGLE):
            continue
        if f" {k} " in h:
            hits[t] = "exact"
            continue
        if len(toks) >= 3:
            pre = " ".join(toks[:2])
            if len(pre) >= 12 and f" {pre} " in h:
                hits.setdefault(t, "partial")
    return list(hits.items())


def match_name(name: str, idx: dict[str, str], keys: list[str]):
    """Exact-or-fuzzy match of a single legal name (USAspending style)."""
    n = norm(name)
    if not n:
        return None, None
    if n in idx:
        return idx[n], "exact"
    close = difflib.get_close_matches(n, keys, n=1, cutoff=0.93)
    if close:
        return idx[close[0]], f"fuzzy~{close[0]}"
    return None, None


# ---- universe ----
@dataclass
class Co:
    ticker: str
    name: str
    cik: int | None
    mcap: float | None
    price: float | None
    volume: float | None
    pct: float | None


def _num(s):
    if s is None:
        return None
    s = re.sub(r"[^0-9.\-]", "", str(s))
    try:
        return float(s) if s not in ("", "-", ".") else None
    except ValueError:
        return None


def _nasdaq_rows():
    try:
        r = get(
            "https://api.nasdaq.com/api/screener/stocks",
            params={"tableonly": "true", "limit": "25000", "offset": "0", "download": "true"},
            headers={
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://www.nasdaq.com",
                "Referer": "https://www.nasdaq.com/",
            },
            timeout=60,
        )
        d = r.json()["data"]
        return d.get("rows") or d.get("table", {}).get("rows") or []
    except Exception as e:  # noqa: BLE001
        log("nasdaq screener unavailable:", e)
        return None


def _watchlist() -> list[str]:
    try:
        with open("watchlist.txt") as f:
            return [x.strip().upper() for x in f if x.strip() and not x.startswith("#")]
    except FileNotFoundError:
        return []


def load_universe() -> dict[str, Co]:
    sec = get("https://www.sec.gov/files/company_tickers.json", sec=True).json()
    by_ticker = {
        row["ticker"].upper(): (row["title"], int(row["cik_str"])) for row in sec.values()
    }
    cos: dict[str, Co] = {}
    rows = _nasdaq_rows()
    if rows:
        for r in rows:
            sym = (r.get("symbol") or "").strip().upper().replace("/", "-")
            mcap, price = _num(r.get("marketCap")), _num(r.get("lastsale"))
            if not sym or not mcap or not price:
                continue
            if not (MIN_MCAP <= mcap <= MAX_MCAP) or price < MIN_PRICE:
                continue
            title, cik = by_ticker.get(sym, (r.get("name") or "", None))
            cos[sym] = Co(sym, title, cik, mcap, price, _num(r.get("volume")), _num(r.get("pctchange")))
        return cos
    # Fallback: only a watchlist.txt (one ticker per line), enriched from Yahoo.
    wl = _watchlist()
    if not wl:
        raise RuntimeError("Nasdaq screener blocked and no watchlist.txt to fall back on")
    import yfinance as yf
    for sym in wl:
        try:
            fi = yf.Ticker(sym).fast_info
            title, cik = by_ticker.get(sym, (sym, None))
            cos[sym] = Co(sym, title, cik, fi.get("market_cap"), fi.get("last_price"), fi.get("last_volume"), None)
        except Exception:  # noqa: BLE001
            continue
    return cos


def name_index(cos: dict[str, Co]) -> dict[str, str]:
    idx: dict[str, str] = {}
    for co in sorted(cos.values(), key=lambda c: -(c.mcap or 0)):
        k = norm(co.name)
        if k:
            idx.setdefault(k, co.ticker)
    return idx


# ---- notifications ----
def send_sms(body: str) -> bool:
    if not (TW_SID and TW_TOKEN and TW_FROM and SMS_TO):
        return False
    r = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{TW_SID}/Messages.json",
        auth=(TW_SID, TW_TOKEN),
        data={"From": TW_FROM, "To": SMS_TO, "Body": body[:1500]},
        timeout=30,
    )
    if r.status_code >= 300:
        log("SMS failed:", r.status_code, r.text[:200])
        return False
    return True


def send_telegram(text: str) -> bool:
    if not (TG_TOKEN and TG_CHAT):
        return False
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": text[:4000], "disable_web_page_preview": True},
        timeout=30,
    )
    return r.status_code < 300


def send_ntfy(title: str, text: str) -> bool:
    if not NTFY_TOPIC:
        return False
    r = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=text.encode("utf-8"),
        headers={"Title": title.encode("ascii", "ignore").decode(), "Priority": "high"},
        timeout=30,
    )
    return r.status_code < 300


def queue_alert(score: float, title: str, long: str) -> None:
    q = load_json("queue.json", [])
    q.append({"ts": datetime.now(timezone.utc).isoformat(), "score": round(score, 1),
              "title": title, "line": long.split("\n")[0]})
    save_json("queue.json", q[-40:])


def notify(score: float, title: str, short: str, long: str,
           now_et: datetime | None = None, force: bool = False) -> str:
    """Returns 'sent', 'push-only' or 'queued'."""
    log(f"ALERT [{score:.1f}] {title}\n{long}\n---")
    now_et = now_et or datetime.now(ET)
    if force or in_work_window(now_et):
        if score >= SMS_MIN:
            send_sms(short)
        if score >= PUSH_MIN:
            send_telegram(long)
            send_ntfy(title, long)
        return "sent"
    if score >= URGENT_MIN:  # off-hours but exceptional: silent push channels only, never SMS
        send_telegram(long)
        send_ntfy(title, long)
        queue_alert(score, title, long)
        return "push-only"
    queue_alert(score, title, long)
    return "queued"
