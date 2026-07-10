"""OPTIONAL HTML scrapers — the fragile part. Disabled by default.

Feeds (iCal/RSS) and the RA API are stable. Scraping raw HTML is not: the moment
a site changes its markup, the CSS selectors below stop matching and this source
quietly returns nothing (it won't crash the run — every source is wrapped in
try/except — but it also won't error loudly, so treat scraped output as
"verify occasionally").

To use: set `html_scrapers.enabled: true` in config.yaml, then fill in the
selectors for the site you want. Below is a worked TEMPLATE, not a guaranteed
working scraper — inspect the target page's HTML and adjust `SELECTORS`.

Please also check the target site's /robots.txt and terms before enabling, and
keep the schedule gentle (a few runs a day is plenty).
"""
from __future__ import annotations
from datetime import datetime, timezone
import requests

from .base import Event, normalise_category, to_iso

# selectolax is only needed if you actually enable scraping. Imported lazily
# inside _scrape() so a missing optional dependency never breaks the core run.

# --- Per-site config. Duplicate this block per site you want to scrape. --------
SELECTORS = {
    "example-listings": {
        "url": "https://example.org/events",
        "category": "other",
        "is_free": True,
        "row": "article.event",          # each event container
        "title": "h2.event__title",      # relative to row
        "link": "a.event__link",         # href taken from this
        "datetime": "time",              # reads the datetime="" attribute
        "venue": ".event__venue",
    },
}


def fetch(horizon_days: int = 45) -> list[Event]:
    out: list[Event] = []
    for name, cfg in SELECTORS.items():
        try:
            out.extend(_scrape(name, cfg))
        except Exception as exc:
            print(f"  ! HTML scraper '{name}' failed: {exc}")
    return out


def _scrape(name, cfg) -> list[Event]:
    from selectolax.parser import HTMLParser   # pip install selectolax to enable
    html = requests.get(cfg["url"], timeout=30,
                        headers={"User-Agent": "berlin-events/1.0"}).text
    tree = HTMLParser(html)
    events: list[Event] = []
    for row in tree.css(cfg["row"]):
        title_node = row.css_first(cfg["title"])
        if not title_node:
            continue
        title = title_node.text(strip=True)
        link_node = row.css_first(cfg["link"])
        url = link_node.attributes.get("href", "") if link_node else ""
        dt_node = row.css_first(cfg["datetime"])
        start = _parse_dt(dt_node.attributes.get("datetime")) if dt_node else None
        if not start:
            continue
        venue_node = row.css_first(cfg["venue"]) if cfg.get("venue") else None
        events.append(Event(
            title=title,
            start=start,
            venue=venue_node.text(strip=True) if venue_node else None,
            source=f"web:{name}",
            url=url,
            category=normalise_category(cfg.get("category", "other"), title),
            is_free=cfg.get("is_free"),
        ))
    return events


def _parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return to_iso(dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))
    except Exception:
        return None
