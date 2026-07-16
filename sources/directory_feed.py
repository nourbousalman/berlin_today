"""Drive the aggregator from the verified directory instead of a hand-kept list.

`directory.json` is the single source of truth: every entry carries an `id`,
`name`, `url`, `group`, and — once the verification file has been imported —
optional `rss` / `ical` feed URLs, a `status`, and an `always_free` flag.

From that one file we derive three things:
  • feed sources   — every entry that has an rss/ical feed becomes a source
                     (category inferred from its group), so no feed URL is ever
                     typed twice.
  • always_free    — the "visit any time, no ticket" venues (own tab).
  • manual_check   — everything we can't auto-ingest (no feed, or bot-walled /
                     parked / broken), i.e. the hand-check worklist.

Until feeds are imported (no entry has rss/ical yet) the feed + manual_check
builders return empty and the aggregator falls back to config.yaml feeds, so the
pipeline keeps working today.
"""
from __future__ import annotations
from pathlib import Path
import json
import re

ROOT = Path(__file__).parent.parent
DIRECTORY = ROOT / "directory.json"

# group text (case-insensitive substring) -> category. First match wins.
_GROUP_RULES = [
    (("nightlife", "club"), "nightlife"),
    (("jam", "choir", "church", "concert", "music"), "music"),
    (("sport", "outdoor"), "sport"),
    (("market", "flea", "swap"), "market"),
    (("gallery", "galleries", "museum", "art", "theatre", "cinema", "film",
      "literature", "opera", "dance", "performance", "foundation", "festival"), "art"),
    (("community", "neighbourhood", "library", "libraries", "garden", "queer",
      "lgbtq", "universit", "institute", "maker", "hacker", "science",
      "bookshop", "language", "cultural centre"), "community"),
]


def group_category(group: str) -> str:
    g = (group or "").lower()
    for needles, cat in _GROUP_RULES:
        if any(n in g for n in needles):
            return cat
    return "other"


# group text (case-insensitive substring) -> free disposition. First match wins.
#   True  = treat as free unless the event text states a price   (free-by-nature)
#   False = treat as paid; kept only if a price <= max_price is found (drop expensive)
#   None  = unknown; decide purely from the event's own text
# Explicit price/"free" wording in an event always overrides this default.
_GROUP_FREE_RULES = [
    # clearly ticketed → paid
    (("club", "nightlife"), False),
    (("opera", "theatre", "theater"), False),
    # genuinely mixed → leave to the text
    (("cinema", "kino"), None),
    (("aggregator", "listings", "listing"), None),
    (("independent culture venue",), None),
    (("music, jams", "music venues", "live-music", "jams &"), None),
    (("performance",), None),
    # free-by-nature (community / civic / free-entry culture)
    (("free", "kostenlos"), True),
    (("librar",), True),
    (("garden",), True),
    (("communit", "neighbourhood", "neighborhood", "social"), True),
    (("queer", "lgbtq"), True),
    (("maker", "hacker", "science"), True),
    (("choir",), True),
    (("church",), True),
    (("swap", "market", "flohmarkt", "flea"), True),
    (("museum", "memorial"), True),
    (("galler",), True),
    (("kunstverein", "non-profit art", "foundation"), True),
    (("cultural institute", "institute", "language"), True),
]


def group_free(group: str):
    """Free/paid/unknown default for a source, inferred from its directory group."""
    g = (group or "").lower()
    for needles, disp in _GROUP_FREE_RULES:
        if any(n in g for n in needles):
            return disp
    return None


def load_directory() -> list[dict]:
    if not DIRECTORY.exists():
        return []
    return json.loads(DIRECTORY.read_text(encoding="utf-8"))


def has_feeds(directory: list[dict]) -> bool:
    """True once the verification file has been imported (any feed recorded)."""
    return any(e.get("rss") or e.get("ical") for e in directory)


def build_feeds(directory: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (ics_feeds, rss_feeds) lists in the shape the source modules expect."""
    ics: list[dict] = []
    rss: list[dict] = []
    for e in directory:
        cat = group_category(e.get("group", ""))
        # community houses / libraries / gardens list standing offers -> recurring
        recurring = cat == "community"
        free = e.get("free", group_free(e.get("group", "")))  # per-entry override, else group default
        if e.get("ical"):
            ics.append({"name": e["name"], "url": e["ical"], "category": cat, "is_free": free})
        if e.get("rss"):
            rss.append({"name": e["name"], "url": e["rss"],
                        "category": cat, "recurring": recurring, "is_free": free})
    return ics, rss


def build_always_free(directory: list[dict]) -> list[dict]:
    out = []
    for e in directory:
        if not e.get("always_free"):
            continue
        out.append({
            "id": e["id"],
            "name": e["name"],
            "url": e["url"],
            "category": group_category(e.get("group", "")),
            "area": e.get("area"),
            "address": e.get("address"),
            "opening_hours": e.get("opening_hours"),   # OSM syntax; None until sourced
            "note": e.get("note") or "",
        })
    out.sort(key=lambda x: x["name"].lower())
    return out


_BLOCKED = {"blocked", "parked", "broken"}


def build_manual_check(directory: list[dict]) -> list[dict]:
    """Entries we can't auto-ingest: no feed, or reachable-but-unscrapeable.

    Gated on feeds having been imported — otherwise every entry would look
    feed-less and the whole directory would land here.
    """
    if not has_feeds(directory):
        return []
    out = []
    for e in directory:
        feed = bool(e.get("rss") or e.get("ical"))
        status = (e.get("status") or "").lower()
        if feed and status not in _BLOCKED:
            continue                       # auto-ingested and healthy -> not manual
        if status == "parked":
            reason = "parked / dead domain"
        elif status == "broken":
            reason = "reachable but broken"
        elif status == "blocked":
            reason = "bot-walled (no automated access)"
        else:
            reason = "no feed"
        out.append({
            "id": e["id"],
            "name": e["name"],
            "url": e["url"],
            "group": e.get("group", ""),
            "reason": reason,
        })
    out.sort(key=lambda x: (x["group"], x["name"].lower()))
    return out
