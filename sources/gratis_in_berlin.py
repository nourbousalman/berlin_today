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
from pathlib import Path
import json
import time
import ssl
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")   # not a fixed +02:00: winter time is +01:00

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


# Each item on a day page carries its own date line ("Jeden 2. Montag",
# "Jeden letzten Freitag", "Montag 02.11.2026"). The site lists a "2nd Monday"
# item on EVERY Monday, so that line decides which days it really happens on.
_WD = {"montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3, "freitag": 4,
       "samstag": 5, "sonntag": 6}
_WD_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_NTH_WORD = {"ersten": 1, "zweiten": 2, "dritten": 3, "vierten": 4, "fünften": 5}
_ORD_EN = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th"}


def _rule(line: str):
    """('nth', {1,3}, weekday) / ('last', None, weekday) / None for anything else."""
    t = re.sub(r"\s+", " ", (line or "").lower()).strip()
    if not t.startswith("jeden "):
        return None
    rest = t[6:]
    wd = next((_WD[w] for w in re.findall(r"[a-zäöüß]+", rest) if w in _WD), None)
    if wd is None:
        return None
    if rest.startswith("letzten"):
        return ("last", None, wd)
    nums = {int(n) for n in re.findall(r"(\d)\.", rest)}
    nums |= {_NTH_WORD[w] for w in re.findall(r"[a-zäöüß]+", rest) if w in _NTH_WORD}
    return ("nth", nums, wd) if nums else None


def _rule_text(rule) -> str:
    kind, nums, wd = rule
    if kind == "last":
        return f"Every last {_WD_EN[wd]} of the month"
    return f"Every {' & '.join(_ORD_EN.get(n, str(n)) for n in sorted(nums))} {_WD_EN[wd]} of the month"


def _occurs(rule, day: datetime) -> bool:
    kind, nums, wd = rule
    if day.weekday() != wd:
        return False
    if kind == "last":
        return (day + timedelta(days=7)).month != day.month
    return (day.day - 1) // 7 + 1 in nums


def _items(html: str):
    """(href, title, date line) for every item on a day page."""
    blocks = re.split(r'<li class="c\b', html)
    found = []
    for b in blocks[1:]:
        m = _ITEM.search(b)
        if not m:
            continue
        d = re.search(r'class="dateTipp">(.*?)</div>', b, re.S)
        found.append((m.group(1), m.group(2), _clean(d.group(1)) if d else ""))
    if not found:                          # layout changed: fall back to titles only
        found = [(h, t, "") for h, t in _ITEM.findall(html)]
    return found


def _day(day: datetime) -> list[Event]:
    url = f"{BASE}/kalender/tagestipps/{day.year}-{day.month}-{day.day}"
    html = _get(url)
    if not html:
        return None                       # refused or unreachable -> caller may reuse last good copy
    out, seen = [], set()
    for href, raw_title, line in _items(html):
        title = _clean(raw_title)
        if not title or _SKIP.match(title) or title.lower() in seen:
            continue
        rule = _rule(line)
        if rule and not _occurs(rule, day):
            continue                      # e.g. a "2nd Monday" item shown on the 1st Monday
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
            recurrence=_rule_text(rule) if rule else None,
            # the date is the page we requested, not a guess
            date_source="daypage" if (hh is not None) else "daypage-allday",
        ))
    return out


_DAYS = Path(__file__).resolve().parent.parent / "docs" / ".gib_days.json"


def fetch(horizon_days: int = 60, max_workers: int = 3, max_days: int = 45) -> list[Event]:
    """One page per day. If the site refuses a day page, reuse that day's events
    from the last successful read, so a temporary refusal never empties the site."""
    import dataclasses
    today = datetime.now(BERLIN).replace(hour=0, minute=0, second=0, microsecond=0)
    days = [today + timedelta(days=i) for i in range(min(horizon_days, max_days))]
    try:
        last = json.loads(_DAYS.read_text("utf-8"))
    except Exception:
        last = {}
    fields = {f.name for f in dataclasses.fields(Event)}
    events: list[Event] = []
    fresh = reused = lost = 0
    keep = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for day, evs in zip(days, ex.map(_day, days)):
            key = day.strftime("%Y-%m-%d")
            if evs is None:
                if last.get(key):
                    events.extend(Event(**{k: v for k, v in c.items() if k in fields}) for c in last[key])
                    keep[key] = last[key]
                    reused += 1
                else:
                    lost += 1
                continue
            fresh += 1
            events.extend(evs)
            keep[key] = [dataclasses.asdict(e) for e in evs]
            time.sleep(0.3)
    try:
        _DAYS.write_text(json.dumps(keep, ensure_ascii=False), "utf-8")
    except Exception:
        pass
    print(f"  ✓ gratis-in-berlin: {len(events)} free events across {len(days)} days "
          f"({fresh} days read, {reused} refused but reused from last read, {lost} refused with no copy)")
    if reused or lost:
        print("  !! gratis-in-berlin refused some pages — fetching gently; it usually recovers")
    return events


# ------------------------------------------------------------------ prices
# The site lists only free things, but the event text sometimes says more:
# "spendenbasiert: Empfehlung 10€", "Spende erwünscht, Vorschlag: 5-10 €",
# "Eintritt 8 €". Show that instead of a bare "Free".
_DONATION = re.compile(r"spende|spenden|hutkasse|\bhut\b|pay what you|donation|auf spendenbasis", re.I)
_SUGGEST = re.compile(r"(?:empfehlung|vorschlag|empfohlen|richtwert|suggested)[^0-9€]{0,25}"
                      r"(\d{1,3}(?:[.,]\d{1,2})?)(?:\s*[-–]\s*(\d{1,3}(?:[.,]\d{1,2})?))?\s*(?:€|euro|eur)", re.I)
_FEE = re.compile(r"(?:eintritt|ticket|tickets|kosten|teilnahmegebühr|gebühr|preis|beitrag)\s*:?\s*"
                  r"(?:von\s+|ab\s+|nur\s+|je\s+)?(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:€|euro\b|eur\b)", re.I)
_FREE_WORDS = re.compile(r"eintritt\s*(?:ist\s*)?(?:frei|kostenlos|gratis)|kostenlos|kostenfrei|gratis|free", re.I)


def _num(x: str) -> float:
    return float(x.replace(",", "."))


# only wording that is about the event itself ("Kuchen sind nicht gratis" or
# "Rodeln kostenpflichtig" is about extras, not the entry)
_NOT_FREE = re.compile(r"not a free (?:show|event|gig|concert)|this (?:show|event) is not free|"
                       r"keine kostenlose veranstaltung|kein gratis-?(?:konzert|event)", re.I)
_EUR = re.compile(r"(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:€|euro\b|eur\b)|€\s*(\d{1,3}(?:[.,]\d{1,2})?)", re.I)
_FEE_EN = re.compile(r"(?:seats?|tickets?|entry|admission|entrance)\s*(?:are|is|costs?|:)?\s*"
                     r"(?:€\s*(\d{1,3}(?:[.,]\d{1,2})?)|(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:€|euro\b|eur\b))", re.I)
_NOT_DONATION = re.compile(r"gespendet|aus spenden|blutspende|spendenlauf|unseren spenden|spendensammlung", re.I)


def _price_from_page(e, note, desc) -> None:
    txt = " ".join(x for x in (note, desc) if x)
    if not txt:
        return
    if _NOT_FREE.search(txt):                 # "NOT A FREE SHOW. Seats are 10€"
        m = _EUR.search(txt)
        e.is_free = False
        if m:
            amt = m.group(1) or m.group(2)
            e.price, e.price_value = f"€{amt}", _num(amt)
        else:
            e.price = None
        return
    en = _FEE_EN.search(txt)
    if en and not _DONATION.search(txt[max(0, en.start() - 60):en.end() + 60]):
        amt = en.group(1) or en.group(2)
        if _num(amt) > 0:
            e.is_free, e.price, e.price_value = False, f"€{amt}", _num(amt)
            return
    if _DONATION.search(_NOT_DONATION.sub(" ", txt)):
        m = _SUGGEST.search(txt)
        if m:
            a = m.group(1).replace(".", ",")
            amt = f"€{a}–{m.group(2).replace('.', ',')}" if m.group(2) else f"€{a}"
            e.price = f"Free — suggested donation {amt}"
        else:
            e.price = "Free — donation welcome"
        e.is_free = True
        return
    fee = _FEE.search(txt)
    if fee and not _FREE_WORDS.search(txt[max(0, fee.start() - 40):fee.end() + 40]):
        v = _num(fee.group(1))
        if v > 0:
            e.is_free = False                 # the event's own text names a price
            e.price = f"€{fee.group(1)}"
            e.price_value = v


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
    # The event's own header: title, address, conditions, date, "Anfangszeit".
    # Only this block is read for time and place — the page also lists other
    # events in a sidebar ("Neue Nationalgalerie (16 - 20 Uhr frei)"), and
    # reading those once gave hundreds of events a wrong 16:00–20:00.
    k = text.find("Kategorie:")
    head_end = k if k != -1 else min(len(text), 1500)
    ics = text.rfind("In meinen Kalender importieren (ICS)", 0, head_end)
    head = text[ics + 36:head_end] if ics != -1 else text[max(0, head_end - 700):head_end]
    st = re.search(r"Anfangszeit:\s*([0-2]?\d)[:.]([0-5]\d)\s*Uhr", head)
    if st and int(st.group(1)) <= 23:
        out["h"], out["m"] = int(st.group(1)), int(st.group(2))
        rng = _TIME_RANGE.search(head)
        if rng and int(rng.group(1)) == out["h"]:
            # the page's own hours ("09:00-17:00 Uhr") win over an odd start field
            # ("Anfangszeit: 09:17" on a 9-to-17 exhibition)
            out["m"] = int(rng.group(2) or 0)
            out["eh"], out["em"] = int(rng.group(3)), int(rng.group(4) or 0)
    else:
        hh, mm, end = _times(head)
        if hh is not None:
            out["h"], out["m"] = hh, mm
            if end:
                out["eh"], out["em"] = end
    # Only accept something that actually looks like a Berlin street address.
    # Anything looser matched page chrome ("Exportieren (ICS)"), and a wrong
    # venue is worse than none.
    addr = re.search(
        r"([A-ZÄÖÜ][\wäöüß.\-]{2,32}(?:stra(?:ss|ß)e|str\.|allee|platz|damm|weg|ufer)"
        r"\s*\d{1,3}[a-z]?(?:[^\w]{0,4}1[0-4]\d{3})?)", head)
    if addr:
        out["venue"] = addr.group(1).strip()[:90]
    # The event description: everything after "Kategorie: <category>" up to the
    # author line. This is what lets a reader understand an insider title like
    # "Adenauer auf der Wolke" (an exhibition of 47 cartoonists' drawings).
    k = text.find("Kategorie:")
    if k != -1:
        rest = text[k + len("Kategorie:"):].strip()
        cat = next((c for c in _GIB_CATS if rest.startswith(c)), None)
        body = rest[len(cat):].strip() if cat else rest
        ends = [i for i in (body.find(m) for m in _DESC_END) if i != -1]
        out["cat"] = cat
        out["desc"] = body[:min(ends) if ends else len(body)].strip()[:900]
    # "Einschränkung" is the site's field for conditions: opening hours,
    # registration required, age limits.
    m = re.search(r"Einschränkung:\s*(.{3,200}?)(?=\s+(?:Montag|Dienstag|Mittwoch|Donnerstag|"
                  r"Freitag|Samstag|Sonntag|Jeden|Anfangszeit|Kategorie)\b)", text)
    if m:
        out["note"] = m.group(1).strip()
    out["v"] = 3
    return out


# gratis-in-berlin's own categories, longest first so "Kinder & Jugendliche"
# wins over a shorter prefix.
_GIB_CATS = sorted(["Special Sightseeing", "Tolle Touren", "Festivals", "Aussichtspunkte",
    "Feuerwerk", "Kino", "Museen", "Kunst", "Lesungen & Hörspiele", "Public Viewing",
    "Comedy & Theater", "Kleinkunst", "International", "Musik", "Wissen live",
    "Leute treffen", "Kinder & Jugendliche", "Wellness", "Schatztruhe", "Strandbars",
    "Outdoor", "Spiele", "Sport", "Partys", "Tanzkurse", "'Mehr' als gratis, aber genial"],
    key=len, reverse=True)
_DESC_END = (" von: ", "Mehr Infos im Internet", "Kommentar schreiben",
             "Weitere interessante Angebote")


def enrich(events: list, budget: int = 250, max_workers: int = 3) -> dict:
    """Fill in real times/venues for day-page events, within a per-run budget."""
    try:
        cache = _json.loads(_DCACHE.read_text("utf-8"))
    except Exception:
        cache = {}
    targets = [e for e in events if e.source.startswith("gib:") and (e.url or "").startswith("http")]
    todo, seen = [], set()
    for e in targets:
        if (e.url in cache and cache[e.url].get("v") == 3) or e.url in seen:
            continue
        if len(todo) >= budget:
            break
        seen.add(e.url)
        todo.append(e.url)

    # Be a polite guest: few connections and a pause. A burst of 10 parallel
    # requests got us "503 Service Unavailable" from this site.
    def work(u):
        try:
            res = _detail(u)
            time.sleep(0.4)
            return u, res, res is not None      # a refused page is retried next run
        except Exception:
            return u, None, False

    refused = 0
    if todo:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            # in small chunks, so we can stop as soon as the site starts refusing
            for i in range(0, len(todo), 9):
                got = 0
                for u, res, ok in ex.map(work, todo[i:i + 9]):
                    if ok:
                        cache[u] = res or {}
                        got += 1
                refused += 9 - got if got < 9 else 0
                if got == 0:
                    print("  !! gratis-in-berlin is refusing event pages — stopping early, will retry next run")
                    break
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
        if d.get("desc") or d.get("note"):
            e.detail_text = " ".join(x for x in (d.get("note"), d.get("desc")) if x)
        if d.get("cat"):
            e.site_category = d["cat"]
        if d.get("venue") and not e.venue:
            e.venue = d["venue"]
        _price_from_page(e, d.get("note"), d.get("desc"))
        # times read before v3 could come from the sidebar (a wrong 16:00–20:00):
        # never use them; the page is re-read and the real time filled in then
        if "h" in d and d.get("v", 0) >= 3:
            base = datetime.fromisoformat(e.start).astimezone(BERLIN)
            mins = d.get("m", 0)
            if "eh" in d and mins and mins == d["eh"]:
                mins = d.get("em", 0)          # "Anfangszeit 09:17" on a 09:00-17:00 page
            end_h, end_m = d.get("eh"), d.get("em", 0)
            if end_h is None and d["h"] < mins <= 23 and mins % 5:
                # the site's editors often type an hour range into the minute field:
                # "Anfangszeit 16:18" with "16 bis 18 Uhr" in the text means 16:00-18:00
                end_h, end_m, mins = mins, 0, 0
            e.start = base.replace(hour=d["h"], minute=mins).isoformat()
            if end_h is not None:
                en = base.replace(hour=min(end_h, 23), minute=end_m)
                if en > datetime.fromisoformat(e.start):
                    e.end = en.isoformat()
            e.date_source = "daypage"
            filled += 1
    print(f"  ✓ gratis-in-berlin detail: {filled} events now have a real time "
          f"({len(cache)} pages cached)")
    return {"filled": filled, "cached": len(cache)}
