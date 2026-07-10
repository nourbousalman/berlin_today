"""Resident Advisor events via the public ra.co GraphQL API (no auth required).

RA is the most reliable structured source for Berlin's club / electronic scene.
It is unofficial — be a good citizen (this script hits it a few times a day, not
in a loop). If RA changes its schema this module may need a tweak; because every
source runs inside its own try/except in aggregate.py, a failure here never breaks
the rest of the feed.
"""
from __future__ import annotations
from datetime import datetime, timedelta
import requests

from .base import Event

ENDPOINT = "https://ra.co/graphql"

_QUERY = """
query GET_EVENT_LISTINGS($filters: FilterInputDtoInput, $filterOptions: FilterOptionsInputDtoInput, $page: Int, $pageSize: Int) {
  eventListings(filters: $filters, filterOptions: $filterOptions, pageSize: $pageSize, page: $page) {
    data {
      id
      listingDate
      event {
        id
        title
        date
        startTime
        endTime
        contentUrl
        flyerFront
        isTicketed
        venue { id name contentUrl live }
        artists { id name }
      }
    }
    totalResults
  }
}
"""

_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Referer": "https://ra.co/events",
    "Origin": "https://ra.co",
}


def fetch(area_id: int = 34, horizon_days: int = 45, max_pages: int = 6) -> list[Event]:
    """area_id 34 == Berlin. Verify at ra.co/events/de/berlin if results look wrong."""
    today = datetime.now().date()
    end = today + timedelta(days=horizon_days)
    events: list[Event] = []

    for page in range(1, max_pages + 1):
        variables = {
            "filters": {
                "areas": {"eq": area_id},
                "listingDate": {"gte": today.isoformat(), "lte": end.isoformat()},
            },
            "filterOptions": {"genre": True},
            "pageSize": 50,
            "page": page,
        }
        resp = requests.post(
            ENDPOINT,
            json={"operationName": "GET_EVENT_LISTINGS", "query": _QUERY, "variables": variables},
            headers=_HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        listings = (payload.get("data") or {}).get("eventListings") or {}
        rows = listings.get("data") or []
        if not rows:
            break
        for row in rows:
            ev = _map(row.get("event") or {})
            if ev:
                events.append(ev)
        if len(rows) < 50:
            break
    return events


def _map(e: dict) -> Event | None:
    title = e.get("title")
    if not title:
        return None
    venue = (e.get("venue") or {}).get("name")
    content = e.get("contentUrl") or ""
    url = f"https://ra.co{content}" if content.startswith("/") else content
    start = e.get("startTime") or e.get("date")
    return Event(
        title=title,
        start=start,
        end=e.get("endTime"),
        venue=venue,
        area="Berlin",
        source="Resident Advisor",
        url=url,
        category="nightlife",
        is_free=None,                                   # RA doesn't expose free-ness
        price="ticketed" if e.get("isTicketed") else None,
        image=e.get("flyerFront"),
    )
