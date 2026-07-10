"""Ingest any number of iCal (.ics / webcal) feeds.

This is the most robust source type: iCal is a fixed standard, so these feeds
almost never break. Point it at venue calendars, org calendars, or any
"Add to calendar / Subscribe" .ics link. Each feed carries a category + a
default is_free flag; individual events can override the category via their
own CATEGORIES property.
"""
from __future__ import annotations
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo
import requests
from icalendar import Calendar

from .base import Event, normalise_category, to_iso

BERLIN = ZoneInfo("Europe/Berlin")


def fetch(feeds: list[dict], horizon_days: int = 45) -> list[Event]:
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=horizon_days)
    out: list[Event] = []
    for feed in feeds or []:
        name = feed.get("name", "calendar")
        url = feed.get("url", "")
        default_cat = feed.get("category", "other")
        is_free = feed.get("is_free")
        try:
            raw = _load(url)
            out.extend(_parse(raw, name, default_cat, is_free, now, horizon))
        except Exception as exc:                       # one bad feed must not sink the rest
            print(f"  ! ICS feed '{name}' failed: {exc}")
    return out


def _load(url: str) -> bytes:
    if "://" in url:                                   # remote feed
        url = url.replace("webcal://", "https://")
        resp = requests.get(url, timeout=30, headers={"User-Agent": "berlin-events/1.0"})
        resp.raise_for_status()
        return resp.content
    with open(url, "rb") as fh:                         # local file (used for the bundled sample)
        return fh.read()


def _parse(raw: bytes, name: str, default_cat: str, is_free, now, horizon) -> list[Event]:
    cal = Calendar.from_ical(raw)
    events: list[Event] = []
    for comp in cal.walk("VEVENT"):
        start_dt = _to_dt(comp.get("dtstart"))
        if start_dt is None or not (now - timedelta(days=1) <= start_dt <= horizon):
            continue
        end_dt = _to_dt(comp.get("dtend"))
        cats = comp.get("categories")
        cat_hint = _first_category(cats)
        title = str(comp.get("summary") or "Untitled")
        events.append(Event(
            title=title,
            start=to_iso(start_dt),
            end=to_iso(end_dt) if end_dt else None,
            venue=str(comp.get("location")) if comp.get("location") else None,
            source=f"cal:{name}",
            url=str(comp.get("url")) if comp.get("url") else "",
            category=normalise_category(cat_hint, default_cat, title),
            is_free=is_free,
            description=_clip(str(comp.get("description") or "") or None),
        ))
    return events


def _to_dt(prop):
    if prop is None:
        return None
    val = prop.dt
    if isinstance(val, datetime):
        # Floating times (no tzinfo) are meant as local Berlin time, not UTC.
        return val if val.tzinfo else val.replace(tzinfo=BERLIN)
    if isinstance(val, date):                           # all-day event
        return datetime(val.year, val.month, val.day, tzinfo=BERLIN)
    return None


def _first_category(cats) -> str:
    if not cats:
        return ""
    try:
        if hasattr(cats, "cats"):
            return str(cats.cats[0])
        return str(cats).split(",")[0]
    except Exception:
        return ""


def _clip(text, limit=280):
    if not text:
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"
