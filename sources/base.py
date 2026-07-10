"""Shared event model + helpers used by every source module."""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import hashlib
import re

# The taxonomy the front-end filters on.
CATEGORIES = ["music", "nightlife", "art", "sport", "community", "market", "other"]

# Keyword -> category. First match wins. Used to normalise free-text tags
# (from ICS CATEGORIES, RSS categories, feed defaults, etc.) into our set.
_CATEGORY_KEYWORDS = [
    ("nightlife", ["club", "rave", "techno", "house", "dj", "party", "nacht", "afterhour"]),
    ("music", ["concert", "konzert", "gig", "live music", "jam", "open mic", "open stage",
               "band", "jazz", "classical", "klassik", "chor", "choir", "song"]),
    ("art", ["art", "kunst", "exhibition", "ausstellung", "gallery", "galerie", "museum",
             "vernissage", "film", "kino", "cinema", "talk", "lesung", "reading", "theatre",
             "theater", "performance"]),
    ("sport", ["sport", "run", "lauf", "yoga", "fitness", "workout", "calisthenics",
               "hike", "wander", "swim", "schwimm", "cycl", "bike", "climb", "bouldern"]),
    ("community", ["language", "sprach", "tandem", "meetup", "stammtisch", "workshop",
                   "kiez", "nachbarschaft", "community", "volunteer", "repair", "swap"]),
    ("market", ["market", "markt", "flohmarkt", "flea", "trödel", "flowmarkt", "bazaar"]),
]


def normalise_category(*hints: str) -> str:
    """Map any free-text hints to one of CATEGORIES."""
    blob = " ".join(h for h in hints if h).lower()
    for cat, words in _CATEGORY_KEYWORDS:
        if any(w in blob for w in words):
            return cat
    return "other"


@dataclass
class Event:
    title: str
    start: str                      # ISO 8601 string, e.g. "2026-07-11T20:00:00+02:00"
    source: str                     # where it came from, e.g. "Resident Advisor"
    url: str = ""
    end: str | None = None
    venue: str | None = None
    area: str | None = None         # neighbourhood / city area if known
    category: str = "other"
    is_free: bool | None = None     # True / False / None (unknown)
    price: str | None = None        # free-text, e.g. "€7", "ticketed", "donation"
    image: str | None = None
    description: str | None = None

    def key(self) -> str:
        """Stable identity for de-duplication (title + day + venue)."""
        day = (self.start or "")[:10]
        raw = f"{_slug(self.title)}|{day}|{_slug(self.venue or '')}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["id"] = self.key()
        return d


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def dedupe(events: list[Event]) -> list[Event]:
    seen: dict[str, Event] = {}
    for ev in events:
        k = ev.key()
        # Prefer the richer record if we see the same event twice.
        if k not in seen or _richness(ev) > _richness(seen[k]):
            seen[k] = ev
    return list(seen.values())


def _richness(ev: Event) -> int:
    return sum(bool(x) for x in (ev.url, ev.venue, ev.image, ev.description, ev.price))


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()
