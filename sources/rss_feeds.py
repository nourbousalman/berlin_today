"""Ingest RSS / Atom event feeds.

Note: RSS is weaker than iCal for events — many feeds carry only a publish date,
not the event's actual date/time. Use it for sources that publish structured
event items; prefer iCal where a site offers both. Feeds can be flagged
`recurring: true` to route their items to the Weekly-regulars section.
"""
from __future__ import annotations
from datetime import datetime, timezone
import time
import feedparser

from .base import Event, normalise_category, to_iso, looks_recurring, looks_free, detect_price


def fetch(feeds: list[dict], horizon_days: int = 45) -> list[Event]:
    out: list[Event] = []
    for feed in feeds or []:
        name = feed.get("name", "feed")
        url = feed.get("url", "")
        default_cat = feed.get("category", "other")
        is_free = feed.get("is_free")
        force_recurring = bool(feed.get("recurring", False))
        try:
            parsed = feedparser.parse(url)
            for entry in parsed.entries:
                ev = _map(entry, name, default_cat, is_free, force_recurring)
                if ev:
                    out.append(ev)
        except Exception as exc:
            print(f"  ! RSS feed '{name}' failed: {exc}")
    return out


def _map(entry, name, default_cat, is_free, force_recurring) -> Event | None:
    title = entry.get("title")
    if not title:
        return None
    when = entry.get("published_parsed") or entry.get("updated_parsed")
    start = to_iso(datetime.fromtimestamp(time.mktime(when), tz=timezone.utc)) if when else None
    if not start:
        return None
    desc = entry.get("summary") or ""
    tags = " ".join(t.get("term", "") for t in entry.get("tags", []))

    recurring = force_recurring or looks_recurring(title, desc)
    price_disp, price_val = detect_price(title, desc)
    if is_free is True:
        free, price_disp, price_val = True, None, None
    elif is_free is False:
        free = False
    elif looks_free(title, desc, tags):
        free, price_disp, price_val = True, None, None
    else:
        free = False if price_val is not None else None

    return Event(
        title=title,
        start=start,
        source=f"rss:{name}",
        url=entry.get("link", ""),
        category=normalise_category(tags, default_cat, title),
        is_free=free,
        price=price_disp,
        price_value=price_val,
        description=_clip(desc),
        recurring=recurring,
        recurrence="Recurring" if recurring else None,
    )


def _clip(text, limit=280):
    if not text:
        return None
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"
