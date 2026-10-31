"""Readers for places on Nour's own list that have a simple page but no feed.

  knoise.org         – the calendar of "K". Only the film nights ("Film im K…"),
                       because that is what is on her list. The page gives date,
                       time and film, but no address and no price, so both are
                       shown as "not listed" instead of being guessed.
  Philharmonie       – the free lunch concerts (Wednesdays 13:00, Sept–June),
                       with the real dates and performers from the official page.

Each reader returns [] when its page changes or is unreachable, so a broken page
never breaks a run. One request per page per run.
"""
from __future__ import annotations

import html as _html
import re
import time
import urllib.request
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .base import Event

BERLIN = ZoneInfo("Europe/Berlin")
_UA = {"User-Agent": "Mozilla/5.0 (berlin_today; free-events list)"}


def _get(url: str, timeout: int = 45) -> str:
    last = None
    for attempt in range(2):                 # one retry: a slow first answer is common
        try:
            req = urllib.request.Request(url, headers=_UA)
            return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")
        except Exception as exc:
            last = exc
            time.sleep(3)
    raise last


def _why_empty(page: str) -> str:
    t = re.search(r"<title>(.*?)</title>", page, re.S | re.I)
    return f"page {len(page)} bytes, title {(_text(t.group(1)) if t else '?')[:60]!r}"


def _text(fragment: str) -> str:
    t = re.sub(r"<br\s*/?>", "\n", fragment)
    t = _html.unescape(re.sub(r"<[^>]+>", " ", t))
    return re.sub(r"[ \t]+", " ", t).strip()


# --------------------------------------------------------------------------- knoise
KNOISE = "https://knoise.org/"


def _year_for(day: int, month: int, today: date) -> int:
    """The page shows dd.mm only. A date more than a month in the past is next year's."""
    y = today.year
    try:
        if date(y, month, day) < date.fromordinal(today.toordinal() - 31):
            y += 1
    except ValueError:
        pass
    return y


def knoise(today: date | None = None) -> list[Event]:
    today = today or datetime.now(BERLIN).date()
    page = _get(KNOISE)
    out, seen = [], set()
    for card in re.findall(r'<article class="event-card[^"]*">(.*?)</article>', page, re.S):
        dm = re.search(r'class="month">\s*(\d{1,2})\.(\d{1,2})\s*<', card)
        tm = re.search(r'class="time">\s*(\d{1,2}):(\d{2})\s*<', card)
        ti = re.search(r'class="event-title">(.*?)</h3>', card, re.S)
        if not (dm and ti):
            continue
        title = _text(ti.group(1))
        if not title.lower().startswith("film im k"):
            continue                      # her list says "knoise film"; concerts are not on it
        d, m = int(dm.group(1)), int(dm.group(2))
        h, mi = (int(tm.group(1)), int(tm.group(2))) if tm else (0, 0)
        try:
            start = datetime(_year_for(d, m, today), m, d, h, mi, tzinfo=BERLIN)
        except ValueError:
            continue
        if (title, start) in seen:        # the page sometimes lists a night twice
            continue
        seen.add((title, start))
        films = [_text(x).strip("“”\"' ") for x in
                 re.findall(r'class="band-name">(.*?)</span>', card, re.S)]
        ev = Event(title=title, start=start.isoformat(), source="web:knoise",
                   url=KNOISE, category="film", is_free=None,
                   price="Not listed on knoise.org", date_source="page")
        ev.detail_text = ("Film screening listed on knoise.org, the calendar of a venue called K. "
                          f"Film: {films[0]}." if films else
                          "Film screening listed on knoise.org, the calendar of a venue called K.")
        ev.price_unlisted = True          # keep it, and say the price is not listed
        out.append(ev)
    if not out:
        print(f"    ! knoise: no film nights found ({_why_empty(page)})")
    return out


# ------------------------------------------------------------- Philharmonie lunch
PHIL = "https://www.berliner-philharmoniker.de/en/concerts/lunch-concerts/"


def philharmonie_lunch() -> list[Event]:
    page = _get(PHIL)
    out = []
    for tile in re.findall(r'<div class="concert-tile">(.*?)(?=<div class="concert-tile">|</section>)',
                           page, re.S):
        dt = re.search(r'<time datetime="(\d{4}-\d{2}-\d{2})">\s*<strong>[^<]*?(\d{1,2}):(\d{2})', tile)
        if not dt:
            continue
        y, mo, d = map(int, dt.group(1).split("-"))
        start = datetime(y, mo, d, int(dt.group(2)), int(dt.group(3)), tzinfo=BERLIN)
        room = re.search(r'<span class="align--right">(.*?)</span>', tile, re.S)
        arts = re.search(r'class="concert-tile__artists">(.*?)</div>', tile, re.S)
        works = re.search(r'class="concert-tile__works[^"]*">(.*?)</p>', tile, re.S)
        who = ""
        if arts:
            lines = [l.strip(" :") for l in _text(arts.group(1)).split("\n") if l.strip(" :")]
            who = "; ".join(lines[:6])
        what = _text(works.group(1)).replace("\n", " ") if works else ""
        venue = "Philharmonie Berlin" + (f", {_text(room.group(1))}" if room else "")
        ev = Event(title="Lunch concert at the Philharmonie", start=start.isoformat(),
                   source="web:Philharmonie lunch concerts", url=PHIL,
                   venue=venue + ", Herbert-von-Karajan-Str. 1", category="music",
                   is_free=True, price="Free", date_source="page")
        # one label covers the whole weekly series, so keep this week's performers out of it
        ev.detail_text = ("Free lunchtime chamber-music concert in the Philharmonie foyer, about 40–50 minutes, "
                          "different musicians each week; catering available. Standing only, first come first served.")
        ev.performers = who
        out.append(ev)
    if not out:
        print(f"    ! Philharmonie: no concerts found ({_why_empty(page)})")
    return out


def fetch() -> list[Event]:
    out: list[Event] = []
    for name, fn in (("knoise", knoise), ("Philharmonie lunch concerts", philharmonie_lunch)):
        try:
            got = fn()
            print(f"    · {name}: {len(got)}")
            out += got
        except Exception as exc:          # a changed or unreachable page must not break the run
            print(f"    ! {name}: {type(exc).__name__}: {exc}")
    return out
