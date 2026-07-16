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
    ("nightlife", ["nightlife", "club", "rave", "techno", "house", "dj", "party", "nacht", "afterhour"]),
    ("music", ["music", "musik", "concert", "konzert", "gig", "live music", "jam", "open mic",
               "open stage", "band", "jazz", "classical", "klassik", "chor", "choir", "song"]),
    ("art", ["art", "kunst", "exhibition", "ausstellung", "gallery", "galerie", "museum",
             "vernissage", "film", "kino", "cinema", "talk", "lesung", "reading", "theatre",
             "theater", "performance"]),
    ("sport", ["sport", "run", "lauf", "yoga", "fitness", "workout", "calisthenics",
               "hike", "wander", "swim", "schwimm", "cycl", "bike", "climb", "bouldern"]),
    ("community", ["community", "language", "sprach", "tandem", "meetup", "stammtisch", "workshop",
                   "kiez", "nachbarschaft", "volunteer", "repair", "swap"]),
    ("market", ["market", "markt", "flohmarkt", "flea", "trödel", "flowmarkt", "bazaar"]),
]


_CATEGORY_SET = set(CATEGORIES)


def normalise_category(*hints: str) -> str:
    """Map any free-text hints to one of CATEGORIES."""
    # 1) Honour an explicit tag if a hint already names a category.
    for h in hints:
        if h and h.strip().lower() in _CATEGORY_SET:
            return h.strip().lower()
    # 2) Otherwise match keywords at word boundaries (so "house" won't fire on
    #    "lighthouse", "run" won't fire on "Brunnen", etc.).
    blob = " ".join(h for h in hints if h).lower()
    for cat, words in _CATEGORY_KEYWORDS:
        if any(re.search(r"\b" + re.escape(w), blob) for w in words):
            return cat
    return "other"


# Strong cadence phrases → treat as recurring even without an iCal RRULE.
_RECUR_KEYWORDS = [
    "every ", "weekly", "wöchentlich", "monthly", "monatlich", "daily", "täglich",
    "jeden ", "jede ", "jeweils", "regelmäßig", "immer ",
    "mondays", "tuesdays", "wednesdays", "thursdays", "fridays", "saturdays", "sundays",
    "montags", "dienstags", "mittwochs", "donnerstags", "freitags", "samstags", "sonntags",
]


def looks_recurring(*hints: str) -> bool:
    blob = " ".join(h for h in hints if h).lower()
    return any(w in blob for w in _RECUR_KEYWORDS)


# Words that clearly mean no-cost entry.
_FREE_KEYWORDS = ["free entry", "free admission", "free of charge", "free ", "for free",
                  "no cover", "kostenlos", "kostenlose", "kostenfrei", "gratis",
                  "eintritt frei", "freier eintritt", "bei freiem eintritt",
                  "eintritt: frei", "umsonst", "ohne eintritt"]

# Pay-what-you-want / donation → treated as free-or-cheap for this planner.
_DONATION_KEYWORDS = ["donation", "pay what you", "pay-what-you", "pwyw",
                      "auf spendenbasis", "spendenbasis", "gegen spende", "spende",
                      "soli-beitrag", "soli "]


def looks_free(*hints: str) -> bool:
    blob = " ".join(h for h in hints if h).lower()
    return any(w in blob for w in _FREE_KEYWORDS)


def looks_donation(*hints: str) -> bool:
    blob = " ".join(h for h in hints if h).lower()
    return any(w in blob for w in _DONATION_KEYWORDS)


import re as _re
_PRICE_RE = _re.compile(
    r'(?:€|eur\b|euro)\s?(\d{1,3}(?:[.,]\d{1,2})?)'      # € 5 / €5,50 / EUR 5
    r'|(\d{1,3}(?:[.,]\d{1,2})?)\s?(?:€|eur\b|euro)'     # 5€ / 5,50 EUR
    r'|(\d{1,3}),-{1,2}\s?(?:€|eur\b|euro)', _re.I)       # 5,- € / 5,-- EUR


def detect_price(*hints: str):
    """Find a price in text. Returns (display_str, min_value) or (None, None)."""
    text = " ".join(h for h in hints if h)
    vals = []
    for m in _PRICE_RE.finditer(text):
        num = (m.group(1) or m.group(2) or m.group(3) or "").replace(",", ".")
        try:
            v = float(num)
            if 0 < v < 1000:
                vals.append(v)
        except ValueError:
            pass
    if not vals:
        return None, None
    lo, hi = min(vals), max(vals)
    disp = f"€{lo:g}" if lo == hi else f"€{lo:g}–{hi:g}"
    return disp, lo


def resolve_free_price(text: str, default_free):
    """(is_free, price_display, price_value) from event text, using the source's
    default only when the text is silent.
    Precedence: explicit 'free' wording > donation/PWYW > explicit price > default."""
    if looks_free(text):
        return True, None, None
    if looks_donation(text):
        return True, "Donation", None
    disp, val = detect_price(text)
    if val is not None:
        return (val <= 0), disp, val           # a stated positive price ⇒ not free
    return default_free, None, None


# Berlin postal codes span 10115–14199. Potsdam (14400+) and every other German
# city fall outside that band, so a PLZ is the most reliable Berlin test.
_BERLIN_DISTRICTS = ("berlin", "kreuzberg", "neukölln", "neukoelln", "friedrichshain",
    "charlottenburg", "wilmersdorf", "schöneberg", "schoeneberg", "wedding", "moabit",
    "prenzlauer berg", "pankow", "lichtenberg", "marzahn", "hellersdorf", "treptow",
    "köpenick", "koepenick", "spandau", "reinickendorf", "steglitz", "zehlendorf",
    "tempelhof", "tiergarten", "gesundbrunnen", "weißensee", "weissensee", "adlershof",
    "mitte", "rummelsburg", "oberschöneweide", "kreuzberg")
_OTHER_CITIES = ("hamburg", "münchen", "munich", "köln", "cologne", "frankfurt",
    "stuttgart", "düsseldorf", "dortmund", "essen", "leipzig", "dresden", "hannover",
    "hanover", "nürnberg", "nuremberg", "bremen", "bonn", "münster", "karlsruhe",
    "mannheim", "wiesbaden", "kiel", "freiburg", "aachen", "mainz", "erfurt", "rostock",
    "kassel", "heidelberg", "potsdam", "eberswalde", "germering", "wien", "vienna",
    "zürich", "zurich", "graz", "linz", "salzburg", "innsbruck", "bielefeld", "wuppertal")
_PLZ_RE = _re.compile(r"\b(\d{5})\b")


def berlin_status(venue: str, *texts: str) -> str:
    """'berlin' | 'other' | 'unknown'.

    PLZ and other-city names are read from the address-like `venue` only (prose in
    titles would false-match, e.g. German 'essen' = to eat). Berlin signals win, so
    an event is dropped only when it positively points elsewhere; events with no
    location text stay 'unknown' and are kept (the source is a Berlin venue)."""
    v = venue or ""
    vlow = v.lower()
    blob = " ".join([v, *[t for t in texts if t]]).lower()
    plz = [int(x) for x in _PLZ_RE.findall(v)]
    berlin_word = ("berlin" in blob or
                   any(_re.search(r"\b" + _re.escape(d) + r"\b", blob) for d in _BERLIN_DISTRICTS))
    if any(10115 <= p <= 14199 for p in plz) or berlin_word:
        return "berlin"
    if any(p < 10115 or p > 14199 for p in plz) or \
       any(_re.search(r"\b" + _re.escape(c) + r"\b", vlow) for c in _OTHER_CITIES):
        return "other"
    return "unknown"


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
    price: str | None = None        # free-text, e.g. "€7", "€8–12", "ticketed"
    price_value: float | None = None  # numeric min price in EUR (None = unknown)
    image: str | None = None
    description: str | None = None
    recurring: bool = False         # True = repeats on a schedule (weekly/monthly regular)
    recurrence: str | None = None   # human-readable cadence, e.g. "Weekly · Wed"
    translated: bool = False        # True if title/description were auto-translated to English

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
