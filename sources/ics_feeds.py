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
from dateutil.rrule import rrulestr

from .base import Event, normalise_category, to_iso, looks_recurring, looks_free, detect_price

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
        force_recurring = bool(feed.get("recurring", False))
        try:
            raw = _load(url)
            out.extend(_parse(raw, name, default_cat, is_free, force_recurring, now, horizon))
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


def _parse(raw, name, default_cat, is_free, force_recurring, now, horizon) -> list[Event]:
    cal = Calendar.from_ical(raw)
    events: list[Event] = []
    for comp in cal.walk("VEVENT"):
        start_dt = _to_dt(comp.get("dtstart"))
        if start_dt is None:
            continue
        title = str(comp.get("summary") or "Untitled")
        desc = str(comp.get("description") or "")
        rrule = comp.get("rrule")

        if rrule is not None:                              # a true repeating event
            recurring = True
            nxt = _rrule_next(start_dt, rrule, now)
            if nxt is None:                                # rule has no future occurrence
                continue
            display_dt = nxt
            recurrence = _rrule_summary(rrule)
        else:
            recurring = force_recurring or looks_recurring(title, desc)
            display_dt = start_dt
            recurrence = "Recurring" if recurring else None
            # One-off events must fall inside the collection window; recurring
            # standing offers are kept regardless of an old start date.
            if not recurring and not (now - timedelta(days=1) <= start_dt <= horizon):
                continue

        end_dt = _to_dt(comp.get("dtend"))
        cat_hint = _first_category(comp.get("categories"))
        price_disp, price_val = detect_price(title, desc)
        if is_free is True:
            free, price_disp, price_val = True, None, None
        elif is_free is False:
            free = False
        elif looks_free(title, desc, cat_hint):
            free, price_disp, price_val = True, None, None
        else:
            free = False if price_val is not None else None
        events.append(Event(
            title=title,
            start=to_iso(display_dt),
            end=to_iso(end_dt) if (end_dt and not recurring) else None,
            venue=str(comp.get("location")) if comp.get("location") else None,
            source=f"cal:{name}",
            url=str(comp.get("url")) if comp.get("url") else "",
            category=normalise_category(cat_hint, default_cat, title),
            is_free=free,
            price=price_disp,
            price_value=price_val,
            description=_clip(desc or None),
            recurring=recurring,
            recurrence=recurrence,
        ))
    return events


_DOW = {"MO": "Mon", "TU": "Tue", "WE": "Wed", "TH": "Thu", "FR": "Fri", "SA": "Sat", "SU": "Sun"}
_ORD = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th", -1: "last"}


def _rrule_next(start_dt, rrule_prop, now):
    """Next occurrence at or after `now`, using dateutil to expand the rule."""
    try:
        rule = rrulestr("RRULE:" + rrule_prop.to_ical().decode(), dtstart=start_dt)
        return rule.after(now - timedelta(hours=6), inc=True)   # small grace for today's events
    except Exception:
        return start_dt if start_dt >= now else None


def _rrule_summary(rrule_prop) -> str:
    """Turn an RRULE into readable text, e.g. 'Weekly · Tue' or 'Monthly · 1st Thu'."""
    def first(key, default=None):
        v = rrule_prop.get(key)
        return v[0] if v else default

    freq = str(first("FREQ", "")).upper()
    interval = int(first("INTERVAL", 1) or 1)
    base = {"DAILY": "Daily", "WEEKLY": "Weekly", "MONTHLY": "Monthly", "YEARLY": "Yearly"}.get(freq, freq.title() or "Recurring")
    if interval > 1:
        base = {"WEEKLY": f"Every {interval} weeks", "DAILY": f"Every {interval} days",
                "MONTHLY": f"Every {interval} months"}.get(freq, base)

    days = rrule_prop.get("BYDAY") or []
    parts = []
    for d in days:
        d = str(d)
        code, ordinal = d[-2:], d[:-2]
        label = _DOW.get(code, code)
        if ordinal:
            try:
                label = f"{_ORD.get(int(ordinal), ordinal)} {label}"
            except ValueError:
                pass
        parts.append(label)
    return base + (" · " + ", ".join(parts) if parts else "")


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
