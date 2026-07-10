"""Ingest RSS / Atom event feeds.

Note: RSS is weaker than iCal for events — many feeds carry only a publish date,
not the event's actual date/time. Use it for sources that publish structured
event items; prefer iCal where a site offers both.
"""
from __future__ import annotations
from datetime import datetime, timezone
import time
import feedparser

from .base import Event, normalise_category, to_iso


def fetch(feeds: list[dict], horizon_days: int = 45) -> list[Event]:
    out: list[Event] = []
    for feed in feeds or []:
        name = feed.get("name", "feed")
        url = feed.get("url", "")
        default_cat = feed.get("category", "other")
        is_free = feed.get("is_free")
        try:
            parsed = feedparser.parse(url)
            for entry in parsed.entries:
                ev = _map(entry, name, default_cat, is_free)
                if ev:
                    out.append(ev)
        except Exception as exc:
            print(f"  ! RSS feed '{name}' failed: {exc}")
    return out


def _map(entry, name, default_cat, is_free) -> Event | None:
    title = entry.get("title")
    if not title:
        return None
    when = entry.get("published_parsed") or entry.get("updated_parsed")
    start = to_iso(datetime.fromtimestamp(time.mktime(when), tz=timezone.utc)) if when else None
    if not start:
        return None
    tags = " ".join(t.get("term", "") for t in entry.get("tags", []))
    return Event(
        title=title,
        start=start,
        source=f"rss:{name}",
        url=entry.get("link", ""),
        category=normalise_category(tags, default_cat, title),
        is_free=is_free,
        description=_clip(entry.get("summary")),
    )


def _clip(text, limit=280):
    if not text:
        return None
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"
