"""Ingest RSS / Atom event feeds.

Note: RSS is weaker than iCal for events — many feeds carry only a publish date,
not the event's actual date/time. Use it for sources that publish structured
event items; prefer iCal where a site offers both. Feeds can be flagged
`recurring: true` to route their items to the Weekly-regulars section.

Feeds are fetched concurrently (a thread pool) so a 122-feed run takes seconds,
not minutes. Each feed still runs in its own try/except, so one broken feed only
drops itself.
"""
from __future__ import annotations
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import time
import feedparser

from .base import Event, normalise_category, to_iso, looks_recurring, resolve_free_price


def fetch(feeds: list[dict], horizon_days: int = 45, max_workers: int = 16) -> list[Event]:
    feeds = feeds or []
    if not feeds:
        return []
    out: list[Event] = []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(feeds))) as ex:
        for evs in ex.map(_fetch_one, feeds):
            out.extend(evs)
    return out


def _fetch_one(feed: dict) -> list[Event]:
    name = feed.get("name", "feed")
    url = feed.get("url", "")
    default_cat = feed.get("category", "other")
    is_free = feed.get("is_free")
    force_recurring = bool(feed.get("recurring", False))
    only_loc = (feed.get("only_location") or "").lower()
    evs: list[Event] = []
    try:
        parsed = feedparser.parse(url)
        for entry in parsed.entries:
            ev = _map(entry, name, default_cat, is_free, force_recurring)
            if not ev:
                continue
            if only_loc and only_loc not in (ev.title + " " + (ev.description or "")).lower():
                continue
            evs.append(ev)
    except Exception as exc:
        print(f"  ! RSS feed '{name}' failed: {exc}")
    return evs


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

    recurring = force_recurring  # RSS: no text-based recurrence (blog posts falsely match)
    free, price_disp, price_val = resolve_free_price(f"{title} {desc} {tags}", is_free)

    return Event(
        title=title,
        start=start,
        source=f"rss:{name}",
        url=entry.get("link", ""),
        category=normalise_category(tags, default_cat, title),
        is_free=free,
        price=price_disp,
        price_value=price_val,
        description=None,  # descriptions intentionally not surfaced
        recurring=recurring,
        recurrence="Recurring" if recurring else None,
    )


def _clip(text, limit=280):
    if not text:
        return None
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"
