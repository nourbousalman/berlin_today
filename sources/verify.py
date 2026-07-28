"""Quality gate: nothing reaches the site unless it is a real, dated event.

Two jobs:

1. `is_junk()` — reject entries that are not events at all. Feeds routinely emit
   job adverts, opening hours, navigation labels, service descriptions and
   placeholder strings ("No feed items at the moment"). These have titles, so
   they look like events to a naive pipeline.

2. `resolve_from_page()` — read the *real* start datetime (and price) off an
   event's own page. An RSS item's publish timestamp is not an event time, so
   items whose date came from a feed must be resolved here or dropped. Order of
   trust: schema.org JSON-LD, then a date+time parsed from the page text.

Results are cached by URL in docs/.verify_cache.json.
"""
from __future__ import annotations

import json
import re
import ssl
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .base import looks_free, looks_donation, detect_price

_CACHE = Path(__file__).resolve().parent.parent / "docs" / ".verify_cache.json"
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_UA = {"User-Agent": "Mozilla/5.0 (compatible; berlin-events/1.0)"}

# ---------------------------------------------------------------- junk filter
_JUNK_EXACT = {
    "news", "kontakt", "impressum", "datenschutz", "hello world!", "startseite",
    "aktuelle veranstaltungen", "regelmäßige veranstaltungen", "veranstaltungen",
    "programm", "kalender", "termine", "über uns", "about", "home", "events",
    "no feed items at the moment", "aktuelles", "archiv", "newsletter", "presse",
    "öffnungszeiten", "anfahrt", "team", "jobs", "spenden", "mitglied werden",
}
_JUNK_PAT = re.compile(
    r"^(stadtteil)?bibliothek\s+\w+$|"          # "Bibliothek Tempelhof" = a branch, not an event
    r"^(aus|au)stellungen?$|^ausbildung$|^fahrbibliothek$|"
    r"anmeldung/|benutzerausweis|onlinekatalog|vöbb|voebb|"
    r"^mehr zum|^unsere \w+-news$|^news\b|"
    r"medienrückgabe|ausleihe|verlängern|vormerkung|"
    r"(stellenausschreibung|stellenangebot|stellenanzeige|jobangebot|"
    r"wir suchen|bewerbung|praktikum|ausschreibung|"
    r"öffnungszeiten|opening hours|anfahrt|barrierefreiheit|"
    r"datenschutz|impressum|newsletter|pressemitteilung|"
    r"jahresbericht|satzung|mitgliedsantrag|spendenaufruf|call for donations|"
    r"save the date|vorschau|rückblick|jahresrückblick)", re.I)
# Titles that are a bare year/range or an archive label
_ARCHIVE_PAT = re.compile(r"^\s*(ausstellungen|exhibitions|programm|archiv)?\s*"
                          r"(19|20)\d\d\s*(–|-|bis|to)\s*(19|20)\d\d\s*$", re.I)


def is_junk(title: str, description: str | None = None) -> bool:
    """True if this is clearly not an event."""
    t = (title or "").strip()
    if len(t) < 4:
        return True
    low = t.lower().strip(" .:-–—")
    if low in _JUNK_EXACT:
        return True
    if _JUNK_PAT.search(t) or _ARCHIVE_PAT.match(t):
        return True
    # a title that is only a date, or only punctuation/emoji
    if re.fullmatch(r"[\W\d\s]+", t):
        return True
    return False


# ------------------------------------------------------- date/price from page
_MONTHS = {
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5,
    "juni": 6, "juli": 7, "august": 8, "september": 9, "oktober": 10,
    "november": 11, "dezember": 12,
    "january": 1, "february": 2, "march": 3, "may": 5, "june": 6, "july": 7,
    "october": 10, "december": 12,
}
_NUM_DATE = re.compile(r"\b([0-3]?\d)\.\s*([01]?\d)\.\s*((?:19|20)\d\d)?")
_NAME_DATE = re.compile(r"\b([0-3]?\d)\.?\s*(" + "|".join(_MONTHS) + r")\.?\s*((?:19|20)\d\d)?", re.I)
_ISO_DATE = re.compile(r"\b((?:20)\d\d)-([01]\d)-([0-3]\d)\b")
_TIME = re.compile(r"\b([0-2]?\d)[:.]([0-5]\d)\s*(?:uhr\b|h\b)?", re.I)
_LD = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.I | re.S)


def _get(url: str, timeout: int = 8) -> str:
    try:
        req = urllib.request.Request(url, headers=_UA)
        return urllib.request.urlopen(req, timeout=timeout, context=_CTX).read().decode("utf-8", "ignore")
    except Exception:
        return ""


def _text(html: str) -> str:
    html = re.sub(r"<(script|style|nav|footer)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _jsonld_start(html: str):
    """(startDate, price_value, is_free) from a schema.org Event, if present."""
    for m in _LD.finditer(html):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
                continue
            if not isinstance(node, dict):
                continue
            if "@graph" in node:
                stack.append(node["@graph"])
            types = node.get("@type") or ""
            types = types if isinstance(types, list) else [types]
            if not any("Event" in str(t) for t in types):
                continue
            start = node.get("startDate")
            if not start:
                continue
            offers = node.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            price = offers.get("price") if isinstance(offers, dict) else None
            try:
                pval = float(str(price).replace(",", ".")) if price not in (None, "") else None
            except Exception:
                pval = None
            return str(start), pval, (True if pval == 0 else None)
    return None, None, None


def _parse_date_time(text: str, now: datetime):
    """First plausible future date (+time) mentioned on the page."""
    cands = []
    for m in _ISO_DATE.finditer(text):
        cands.append((int(m.group(1)), int(m.group(2)), int(m.group(3)), m.end()))
    for m in _NUM_DATE.finditer(text):
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        if 1 <= mo <= 12 and 1 <= d <= 31:
            cands.append((int(y) if y else now.year, mo, d, m.end()))
    for m in _NAME_DATE.finditer(text):
        d = int(m.group(1))
        mo = _MONTHS.get(m.group(2).lower())
        y = m.group(3)
        if mo and 1 <= d <= 31:
            cands.append((int(y) if y else now.year, mo, d, m.end()))

    best = None
    for y, mo, d, pos in cands:
        try:
            dt = datetime(y, mo, d, tzinfo=timezone.utc)
        except ValueError:
            continue
        if not y or dt < now.replace(hour=0, minute=0):      # roll a bare date into the future
            try:
                dt = dt.replace(year=dt.year + 1)
            except ValueError:
                continue
        if dt < now.replace(hour=0, minute=0) or (dt - now).days > 400:
            continue
        tm = _TIME.search(text[pos:pos + 120])
        hh, mm = (int(tm.group(1)), int(tm.group(2))) if tm else (0, 0)
        if hh > 23 or mm > 59:
            hh, mm = 0, 0
        dt = dt.replace(hour=hh, minute=mm)
        if best is None or dt < best[0]:
            best = (dt, bool(tm))
    return best


def _load() -> dict:
    try:
        return json.loads(_CACHE.read_text("utf-8"))
    except Exception:
        return {}


def _save(cache: dict) -> None:
    try:
        _CACHE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE.write_text(json.dumps(cache, ensure_ascii=False), "utf-8")
    except Exception:
        pass


def resolve_from_page(url: str, now: datetime | None = None) -> dict | None:
    """Read a real start datetime (and price if stated) from an event page."""
    now = now or datetime.now(timezone.utc)
    html = _get(url)
    if not html:
        return None
    start, pval, free = _jsonld_start(html)
    if start:
        return {"start": start, "date_source": "jsonld",
                "price_value": pval, "is_free": free}
    text = _text(html)
    hit = _parse_date_time(text, now)
    if not hit:
        return None
    dt, had_time = hit
    # A bare date with no time anywhere near it is weak evidence — it is usually
    # an incidental date on a navigation/info page, not an event. Refuse it
    # rather than publish a fabricated 00:00 start.
    if not had_time:
        return None
    out = {"start": dt.isoformat(), "date_source": "page",
           "price_value": None, "is_free": None, "had_time": had_time}
    if looks_free(text) or looks_donation(text):
        out["is_free"] = True
    else:
        disp, val = detect_price(text)
        if val is not None:
            out["price_value"], out["is_free"] = val, (val <= 0)
    return out


def resolve_many(events: list, max_workers: int = 14, budget: int = 250) -> dict:
    """Resolve every event whose date came from a publish timestamp.
    Events that cannot be resolved keep date_source='publish' and must be dropped.
    """
    targets = [e for e in events
               if getattr(e, "date_source", "publish") == "publish"
               and (e.url or "").startswith("http")]
    stats = {"checked": len(targets), "resolved": 0, "failed": 0, "skipped": 0}
    if not targets:
        return stats

    cache = _load()
    now = datetime.now(timezone.utc)
    # Newest posts first — an upcoming event is far likelier to be announced
    # recently. Cached URLs cost nothing, so only *new* fetches use the budget.
    targets.sort(key=lambda e: e.start or "", reverse=True)
    todo, seen = [], set()
    for e in targets:
        if e.url in cache or e.url in seen:
            continue
        if len(todo) >= budget:
            stats["skipped"] += 1
            continue
        seen.add(e.url)
        todo.append(e.url)

    def work(url):
        try:
            return url, resolve_from_page(url, now), True
        except Exception:
            return url, None, False

    if todo:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for url, res, fetched in ex.map(work, todo):
                if fetched:
                    cache[url] = res
    _save(cache)

    for e in targets:
        res = cache.get(e.url)
        if not res:
            stats["failed"] += 1
            continue
        e.start = res["start"]
        e.date_source = res["date_source"]
        if res.get("is_free") is not None and e.is_free is None:
            e.is_free = res["is_free"]
        if res.get("price_value") is not None and e.price_value is None:
            e.price_value = res["price_value"]
            e.price = f"€{res['price_value']:g}"
        stats["resolved"] += 1
    return stats
