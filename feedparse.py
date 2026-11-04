"""Tiny RSS 2.0 / Atom parser on the standard library (no feedparser dependency)."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _ts(s: str | None) -> float | None:
    if not s:
        return None
    s = s.strip()
    try:
        dt = parsedate_to_datetime(s)  # RSS: RFC 822
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))  # Atom: ISO 8601
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def parse_feed(content: bytes | str) -> list[dict]:
    """-> [{title, link, summary, id, ts}] for RSS items and Atom entries."""
    root = ET.fromstring(content if isinstance(content, bytes) else content.encode("utf-8"))
    out = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        d = {"title": "", "link": "", "summary": "", "id": "", "ts": None}
        for ch in node:
            n = _local(ch.tag)
            text = (ch.text or "").strip()
            if n == "title":
                d["title"] = text
            elif n == "link":
                d["link"] = ch.get("href") or text or d["link"]
            elif n in ("description", "summary", "content") and not d["summary"]:
                d["summary"] = text
            elif n in ("guid", "id"):
                d["id"] = text
            elif n in ("pubDate", "published", "updated", "date") and d["ts"] is None:
                d["ts"] = _ts(text)
        out.append(d)
    return out
