"""Orchestrator: gather signals from every source, score per ticker, alert on confluence.

Every signal carries signed points (+ bullish, - bearish). Points decay with age, are
summed per ticker, and an alert fires when a ticker with a NEW signal crosses a threshold.

  python scanner.py            normal run (what the scheduler calls)
  python scanner.py --dry      run everything, print alerts, send nothing, save nothing
  python scanner.py --test     send a test alert through every configured channel
  python scanner.py --sources dod,pr   run only some sources (awards,dod,pr,8k,volume)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from common import (
    PUSH_MIN, SMS_MIN, load_json, load_universe, log, money, name_index, notify, save_json,
    state_exists,
)
from sources_alpaca import refresh_quotes
from sources_awards import dod_signals, usaspending_signals
from sources_news import edgar_signals, pr_signals
from sources_volume import ET, refresh_avgvol, volume_signals

AWARD_HOURS = {int(x) for x in os.getenv("AWARD_HOURS", "12,22").split(",") if x.strip()}  # UTC
MAX_UNIVERSE = 4500


def decay(age_h: float) -> float:
    return 1.0 if age_h <= 12 else 0.6 if age_h <= 36 else 0.3 if age_h <= 72 else 0.0


def score(sigs: list[dict], now: datetime) -> tuple[float, float, float]:
    pos = neg = 0.0
    for s in sigs:
        age = (now - datetime.fromisoformat(s["ts"])).total_seconds() / 3600
        p = s["pts"] * decay(age)
        if p > 0:
            pos += p
        else:
            neg += p
    return pos + neg, pos, neg


def direction(total: float, pos: float, neg: float) -> str:
    if total > 0 and neg > -2:
        return "BULLISH"
    if total < 0 and pos < 2:
        return "BEARISH"
    return "MIXED"


def setup_label(co, sigs, total, pos, neg) -> str:
    """Name the pattern so you know what to check first. Informational, not advice."""
    kinds = {s["kind"] for s in sigs if s["pts"] > 0}
    pct = co.pct if co is not None and co.pct is not None else 0.0
    stealth = any("STEALTH" in s["text"] for s in sigs)
    big_fed = any(s["kind"] in ("DOD", "AWARD") and s["pts"] >= 3 for s in sigs)
    catalyst = bool(kinds & {"DOD", "AWARD", "PR", "8K"})
    if neg <= -3 and total <= -2:
        return "FADE/AVOID: dilution or distress signal. Do not chase."
    if neg <= -3:
        return "CAUTION: good news but dilution/distress risk. Check for an offering first."
    if catalyst and stealth:
        return "NOT PRICED IN?: news plus heavy volume while price barely moved. Highest-interest pattern; read the source."
    if big_fed and abs(pct) < 5:
        return "CONTRACT NOT MOVED YET: large vs market cap, price flat. Check liquidity and spread."
    if catalyst and pct >= 15:
        return f"ALREADY RAN: +{pct:.0f}% already. Chasing risk; consider waiting for a pullback."
    if catalyst:
        return "CATALYST: confirm the price and volume reaction before acting."
    if "VOL" in kinds:
        return "MOMENTUM, NO CATALYST FOUND: check news manually before trusting it."
    return ""


def build_alert(t, co, sigs, total, pos, neg, new_keys):
    d = direction(total, pos, neg)
    mcap = money(co.mcap) if co and co.mcap else "n/a"
    px = f"${co.price:.2f}" if co and co.price else ""
    title = f"{d} {t} {total:+.1f}"
    top = sorted(sigs, key=lambda s: -abs(s["pts"]))[:6]
    lines = [f"{title} | mcap {mcap} {px}"]
    setup = setup_label(co, sigs, total, pos, neg)
    if setup:
        lines.append("SETUP: " + setup)
    for s in top:
        star = "*" if s["key"] in new_keys else " "
        lines.append(f"{star}{s['kind']:5} {s['pts']:+.1f} {s['text'][:230]}")
    url = next((s["url"] for s in top if s.get("url")), "")
    if url:
        lines.append(url)
    if d != "BEARISH" and neg <= -3:
        lines.append("WARNING: dilution/distress signal present. Pumps often get sold into offerings.")
    lines.append("Verify the company match and read the source before acting. * = new this run")
    label = setup.split(":")[0] + " | " if setup else ""
    short = f"{title} | {label}" + "; ".join(s["text"][:60] for s in top[:2]) + (f" | {url}" if url else "")
    return title, short, "\n".join(lines)


def brief_text(kind, now, uni, by_t, queue):
    rows = []
    for t, v in by_t.items():
        tot, pos, neg = score(v, now)
        if abs(tot) >= 2 and t in uni:
            rows.append((tot, t, pos, neg, v))
    bulls = sorted((r for r in rows if r[0] > 0), reverse=True)[:6]
    bears = sorted(r for r in rows if r[0] < 0)[:4]
    head = "GAME PLAN (pre-open)" if kind == "morning" else "WRAP (end of your shift)"
    lines = [head]
    if queue:
        lines.append(f"Queued while you were off ({len(queue)}):")
        lines += [f"  {q['line'][:130]}" for q in queue[:6]]

    def row(r):
        tot, t, pos, neg, v = r
        co = uni[t]
        lab = setup_label(co, v, tot, pos, neg).split(":")[0]
        px = f"${co.price:.2f}" if co.price else ""
        return f"  {t} {tot:+.1f} {px} {lab}"

    if bulls:
        lines += ["Bullish watch:"] + [row(r) for r in bulls]
    if bears:
        lines += ["Bearish / avoid:"] + [row(r) for r in bears]
    if not rows:
        lines.append("No strong signals right now.")
    lines.append("Built from public data; can be stale or mismatched. Verify before acting.")
    picks = bulls[:3] + bears[:1]
    short = head.split(" (")[0] + ": " + (", ".join(f"{r[1]} {r[0]:+.0f}" for r in picks) or "quiet")
    return head, short, "\n".join(lines)


def maybe_brief(now, now_et, uni, by_t, force=None):
    """8:40-9:25 ET game plan and ~14:00 ET wrap, once per day each (or forced)."""
    st = load_json("brief.json", {})
    today = now_et.date().isoformat()
    m = now_et.hour * 60 + now_et.minute
    kind = force
    if not kind and now_et.weekday() < 5:
        if 8 * 60 + 40 <= m < 9 * 60 + 25 and st.get("morning") != today:
            kind = "morning"
        elif 13 * 60 + 55 <= m < 14 * 60 + 40 and st.get("wrap") != today:
            kind = "wrap"
    if not kind:
        return None
    queue = load_json("queue.json", []) if kind == "morning" else []
    head, short, long = brief_text(kind, now, uni, by_t, queue)
    notify(SMS_MIN, head, short, long, now_et=now_et, force=True)
    st[kind] = today
    save_json("brief.json", st)
    if kind == "morning":
        save_json("queue.json", [])
    return kind


def run(args) -> int:
    now = datetime.now(timezone.utc)
    now_et = now.astimezone(ET)
    first_run = not state_exists("seen.json")
    uni = load_universe()
    if not uni:
        log("empty universe")
        return 1
    idx = name_index(uni)
    log(f"universe: {len(uni)} tickers; first_run={first_run}")
    refresh_quotes(uni)  # prices/volume from Alpaca delayed SIP (max ~15 min old); no-op without keys

    seen: dict = load_json("seen.json", {})
    sigs: list[dict] = load_json("signals.json", [])
    alerted: dict = load_json("alerted.json", {})
    av = load_json("avgvol.json", {"asof": None, "data": {}})

    stale = (not av["asof"]) or (now - datetime.fromisoformat(av["asof"])) > timedelta(hours=20)
    wanted_all = {"awards", "dod", "pr", "8k", "volume"}
    if args.sources:
        wanted = set(args.sources.split(","))
    else:
        wanted = {"dod", "pr", "8k", "volume"} | ({"awards"} if now.hour in AWARD_HOURS else set())
    if "volume" in wanted and stale:
        log("refreshing 20-day average volumes (once a day)...")
        data = refresh_avgvol(sorted(uni, key=lambda t: -(uni[t].mcap or 0))[:MAX_UNIVERSE])
        if data:
            av = {"asof": now.isoformat(), "data": data}
            save_json("avgvol.json", av) if not args.dry else None

    srcs = {
        "awards": lambda: usaspending_signals(uni, idx),
        "dod": lambda: dod_signals(uni, idx),
        "pr": lambda: pr_signals(uni, idx),
        "8k": lambda: edgar_signals(uni),
        "volume": lambda: volume_signals(uni, av["data"], now_et),
    }
    new, health = [], {}
    for name in sorted(wanted & wanted_all):
        try:
            got = srcs[name]()
            health[name] = len(got)
            new += got
        except Exception as e:  # noqa: BLE001
            health[name] = f"ERR {type(e).__name__}: {e}"
            log(f"source {name} failed:", e)
    log("source health:", health)
    if health and not any(isinstance(v, int) for v in health.values()):
        log("every source failed")
        return 1

    fresh, keys = [], set()
    for s in new:
        if s.key in seen or s.key in keys:
            continue
        keys.add(s.key)
        fresh.append(s)
    for s in fresh:
        seen[s.key] = now.isoformat()
        sigs.append(asdict(s))
    sigs = [s for s in sigs if (now - datetime.fromisoformat(s["ts"])).total_seconds() < 72 * 3600]
    seen = {k: v for k, v in seen.items() if (now - datetime.fromisoformat(v)).days < 10}

    by_t: dict[str, list[dict]] = {}
    for s in sigs:
        by_t.setdefault(s["ticker"], []).append(s)

    sent = 0
    for t in sorted({s.ticker for s in fresh}):
        total, pos, neg = score(by_t[t], now)
        if abs(total) < PUSH_MIN or first_run:
            continue
        last = alerted.get(t)
        if last:
            age = (now - datetime.fromisoformat(last["ts"])).total_seconds() / 3600
            same_sign = (last["score"] > 0) == (total > 0)
            if age < 24 and same_sign and abs(total) - abs(last["score"]) < 1.5:
                continue
        title, short, long = build_alert(t, uni.get(t), by_t[t], total, pos, neg, keys)
        if args.dry:
            log("[dry]", long)
        else:
            notify(abs(total), title, short, long)
        alerted[t] = {"score": total, "ts": now.isoformat()}
        sent += 1

    if not args.dry:
        hot = sorted(((score(v, now)[0], t) for t, v in by_t.items()), reverse=True)[:30]
        save_json("hot.json", [{"ticker": t, "score": round(sc, 1)} for sc, t in hot if sc > 0])
        save_json("seen.json", seen)
        save_json("signals.json", sigs)
        alerted = {t: v for t, v in alerted.items()
                   if (now - datetime.fromisoformat(v["ts"])).days < 3}
        save_json("alerted.json", alerted)
        # Precomputed view for the website (site/index.html reads this one file)
        rows = []
        for t, v in by_t.items():
            tot, pos, neg = score(v, now)
            if abs(tot) < 1 or t not in uni:
                continue
            co = uni[t]
            rows.append({
                "ticker": t, "name": co.name, "score": round(tot, 1), "dir": direction(tot, pos, neg),
                "setup": setup_label(co, v, tot, pos, neg), "price": co.price, "mcap": co.mcap, "pct": co.pct,
                "signals": [
                    {"kind": s["kind"], "pts": s["pts"], "text": s["text"][:240], "url": s.get("url", ""), "ts": s["ts"]}
                    for s in sorted(v, key=lambda s: -abs(s["pts"]))[:8]
                ],
            })
        rows.sort(key=lambda r: -abs(r["score"]))
        os.makedirs("docs", exist_ok=True)
        with open("docs/dashboard.json", "w") as f:
            json.dump({
                "generated": now.isoformat(), "health": health, "rows": rows[:80],
                "queue": load_json("queue.json", [])[-10:],
            }, f)
    force = "morning" if getattr(args, "brief", False) else "wrap" if getattr(args, "wrap", False) else None
    if not args.dry and (not first_run or force):
        maybe_brief(now, now_et, uni, by_t, force)
    elif args.dry and force:
        log(brief_text(force, now, uni, by_t, load_json("queue.json", []))[2])
    log(f"done: {len(fresh)} new signals, {sent} alerts" + (" (bootstrap: alerts suppressed)" if first_run else ""))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--sources", default="")
    ap.add_argument("--brief", action="store_true", help="send the pre-open game plan now")
    ap.add_argument("--wrap", action="store_true", help="send the end-of-shift wrap now")
    a = ap.parse_args()
    if a.test:
        notify(99, "TEST scanner", "Scanner test: SMS channel works.", "Scanner test: push channels work.", force=True)
        return 0
    return run(a)


if __name__ == "__main__":
    sys.exit(main())
