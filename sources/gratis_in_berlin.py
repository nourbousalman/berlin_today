"""gratis-in-berlin.de — a curated, day-scoped index of FREE Berlin events.

Why this source is different from everything else in the pipeline:

  * It is organised BY DAY. Each page (/kalender/tagestipps/YYYY-M-D) lists what
    is on that day, so the event date comes from the page we asked for — not from
    a publish timestamp we have to guess at. That makes it the most reliable
    date source available short of iCal.
  * Everything on it is free by editorial policy, so price needs no inference.
  * It is human-curated, so the entries are real events rather than blog posts.

We therefore fetch one page per day across the horizon and treat the page's date
as authoritative. Times are parsed out of the title where the editors put them
("18:00h ...", "... | 11 - 17 Uhr"); entries with no stated time are kept as
all-day, which is honest rather than invented.
"""
from __future__ import annotations

import re
import ssl
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from .base import Event, normalise_category

BASE = "https://www.gratis-in-berlin.de"
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_UA = {"User-Agent": "Mozilla/5.0 (compatible; berlin-events/1.0)"}

_ITEM = re.compile(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.{4,160}?)</a>', re.S)
# "18:00h", "20 Uhr", "11 - 17 Uhr", "10:00-18:00"
_TIME = re.compile(r"(?<!\d)([0-2]?\d)(?:[:.]([0-5]\d))?\s*(?:h\b|uhr\b)", re.I)
_TIME_RANGE = re.compile(r"(?<!\d)([0-2]?\d)(?:[:.]([0-5]\d))?\s*[-–]\s*([0-2]?\d)(?:[:.]([0-5]\d))?\s*(?:h\b|uhr\b)", re.I)

# The site's own chrome, and entries that describe a standing offer rather than
# a dated event.
_SKIP = re.compile(
    r"^(berlin kostenlos erleben|home|start|newsletter|impressum|datenschutz|"
    r"neue tipps|dauerbrenner|selber tipp|login|projekt|presse|faq|sponsoring)", re.I)

_CAT_HINTS = [
    (("konzert", "musik", "jam", "chor", "band", "dj", "klavier", "jazz"), "music"),
    (("ausstellung", "galerie", "kunst", "vernissage", "museum", "foto", "film", "kino",
      "lesung", "vortrag", "theater", "comedy", "performance"), "art"),
    (("flohmarkt", "trödel", "markt", "antikmarkt", "tausch"), "markets"),
    (("yoga", "lauf", "sport", "wander", "radtour", "schwimm", "tanz"), "sport"),
    (("workshop", "führung", "sprachcafé", "repair", "treff", "beratung",
      "spiel", "kinder", "familie"), "community"),
]


def _get(url: str, timeout: int = 12) -> str:
    try:
        req = urllib.request.Request(url, headers=_UA)
        return urllib.request.urlopen(req, timeout=timeout, context=_CTX).read().decode("utf-8", "ignore")
    except Exception:
        return ""


def _clean(raw: str) -> str:
    txt = re.sub(r"<[^>]+>", "", raw)
    for a, b in (("&amp;", "&"), ("&quot;", '"'), ("&#039;", "'"), ("&apos;", "'"),
                 ("&nbsp;", " "), ("&ndash;", "–"), ("&bdquo;", "„"), ("&ldquo;", "“")):
        txt = txt.replace(a, b)
    return re.sub(r"\s+", " ", txt).strip()


def _category(title: str) -> str:
    low = title.lower()
    for needles, cat in _CAT_HINTS:
        if any(n in low for n in needles):
            return normalise_category("", cat, title)
    return normalise_category("", "community", title)


def _times(title: str):
    """(start_hh, start_mm, end) parsed from the title, or (None, None, None)."""
    m = _TIME_RANGE.search(title)
    if m:
        return (int(m.group(1)), int(m.group(2) or 0),
                (int(m.group(3)), int(m.group(4) or 0)))
    m = _TIME.search(title)
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        if h <= 23:
            return h, mi, None
    return None, None, None


def _day(day: datetime) -> list[Event]:
    url = f"{BASE}/kalender/tagestipps/{day.year}-{day.month}-{day.day}"
    html = _get(url)
    if not html:
        return []
    out, seen = [], set()
    for href, raw_title in _ITEM.findall(html):
        title = _clean(raw_title)
        if not title or _SKIP.match(title) or title.lower() in seen:
            continue
        seen.add(title.lower())
        hh, mm, end = _times(title)
        start = day.replace(hour=hh or 0, minute=mm or 0, second=0, microsecond=0)
        end_iso = None
        if end:
            e = day.replace(hour=min(end[0], 23), minute=end[1], second=0, microsecond=0)
            if e > start:
                end_iso = e.isoformat()
        link = href if href.startswith("http") else BASE + "/" + href.lstrip("/")
        out.append(Event(
            title=title[:200],
            start=start.isoformat(),
            end=end_iso,
            source="gib:gratis-in-berlin",
            url=link,
            category=_category(title),
            is_free=True,                     # editorial policy of the whole site
            price=None,
            price_value=None,
            description=None,
            recurring=False,
            # the date is the page we requested, not a guess
            date_source="daypage" if (hh is not None) else "daypage-allday",
        ))
    return out


def fetch(horizon_days: int = 60, max_workers: int = 8, max_days: int = 45) -> list[Event]:
    today = datetime.now(timezone(timedelta(hours=2)))
    days = [today + timedelta(days=i) for i in range(min(horizon_days, max_days))]
    events: list[Event] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for evs in ex.map(_day, days):
            events.extend(evs)
    print(f"  ✓ gratis-in-berlin: {len(events)} free events across {len(days)} days")
    return events


# --------------------------------------------------------------- enrichment
# Day pages carry only title + date. Time and venue live on each event's own
# page, so we fetch those progressively: results are cached by URL (the pages are
# static once published) and each run spends a fixed budget on pages it has not
# seen. Over a few runs the whole list gains real times.
import json as _json
from pathlib import Path as _Path

_DCACHE = _Path(__file__).resolve().parent.parent / "docs" / ".gib_cache.json"
_VENUE = re.compile(
    r"(?:Ort|Adresse|Wo)\s*[:\-]?\s*([^|<\n]{6,90})|"
    r"([A-ZÄÖÜ][\wäöüß.\- ]{3,44}(?:stra(?:ss|ß)e|str\.|allee|platz|damm|weg|ufer)\s*\d{1,3}[a-z]?)", re.I)


def _detail(url: str):
    html = _get(url, 10)
    if not html:
        return None
    text = _clean(re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I))
    out = {}
    hh, mm, end = _times(text[:2500])
    if hh is not None:
        out["h"], out["m"] = hh, mm
        if end:
            out["eh"], out["em"] = end
    # Only accept something that actually looks like a Berlin street address.
    # Anything looser matched page chrome ("Exportieren (ICS)"), and a wrong
    # venue is worse than none.
    addr = re.search(
        r"([A-ZÄÖÜ][\wäöüß.\-]{2,32}(?:stra(?:ss|ß)e|str\.|allee|platz|damm|weg|ufer)"
        r"\s*\d{1,3}[a-z]?(?:[^\w]{0,4}1[0-4]\d{3})?)", text[:4000])
    if addr:
        out["venue"] = addr.group(1).strip()[:90]
    return out


def enrich(events: list, budget: int = 250, max_workers: int = 12) -> dict:
    """Fill in real times/venues for day-page events, within a per-run budget."""
    try:
        cache = _json.loads(_DCACHE.read_text("utf-8"))
    except Exception:
        cache = {}
    targets = [e for e in events if e.source.startswith("gib:") and (e.url or "").startswith("http")]
    todo, seen = [], set()
    for e in targets:
        if e.url in cache or e.url in seen:
            continue
        if len(todo) >= budget:
            break
        seen.add(e.url)
        todo.append(e.url)

    def work(u):
        try:
            return u, _detail(u), True
        except Exception:
            return u, None, False

    if todo:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for u, res, ok in ex.map(work, todo):
                if ok:
                    cache[u] = res or {}
    try:
        _DCACHE.parent.mkdir(parents=True, exist_ok=True)
        _DCACHE.write_text(_json.dumps(cache, ensure_ascii=False), "utf-8")
    except Exception:
        pass

    filled = 0
    for e in targets:
        d = cache.get(e.url)
        if not d:
            continue
        if d.get("venue") and not e.venue:
            e.venue = d["venue"]
        if "h" in d:
            base = datetime.fromisoformat(e.start)
            e.start = base.replace(hour=d["h"], minute=d.get("m", 0)).isoformat()
            if "eh" in d:
                en = base.replace(hour=min(d["eh"], 23), minute=d.get("em", 0))
                if en > datetime.fromisoformat(e.start):
                    e.end = en.isoformat()
            e.date_source = "daypage"
            filled += 1
    print(f"  ✓ gratis-in-berlin detail: {filled} events now have a real time "
          f"({len(cache)} pages cached)")
    return {"filled": filled, "cached": len(cache)}
