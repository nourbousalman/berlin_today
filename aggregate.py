#!/usr/bin/env python3
"""Collect Berlin events + directory buckets into docs/data.json.

Each source runs in its own try/except, so one broken feed logs a warning and
the rest still produce a full file. Run locally with `python aggregate.py`; in
production it runs on a schedule via .github/workflows/update.yml.

Output (docs/data.json):
  events        — dated events from every iCal/RSS feed (recurring flag + per-
                  event link preserved). Filtered to free / <=max_price / unknown.
  always_free   — venues you can visit any time for no ticket (own tab).
  manual_check  — sources we can't auto-ingest: no feed, or bot-walled/parked
                  /broken (the hand-check worklist).

Feeds are derived from directory.json once the verification file is imported;
until then we fall back to the feeds listed in config.yaml, plus the local
manual.ics / sample.ics, so the pipeline keeps working.
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
import sys
from collections import Counter
import yaml

import socket
socket.setdefaulttimeout(45)  # one slow/hanging feed must not stall a 122-feed run

from sources.base import Event, dedupe, berlin_status
from sources import resident_advisor, ics_feeds, rss_feeds, html_scrapers, directory_feed, price_probe
from sources.translate import translate_events

ROOT = Path(__file__).parent
OUT = ROOT / "docs" / "data.json"


def load_config() -> dict:
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def resolve_feeds(cfg: dict, directory: list[dict]) -> tuple[list[dict], list[dict]]:
    """Merge config feeds with directory-derived feeds.

    - iCal: keep config's local files (manual.ics / sample.ics) always, and add
      any real .ics feeds discovered in the directory.
    - RSS: use the directory's full set once imported; otherwise the config list.
    """
    dir_ics, dir_rss = directory_feed.build_feeds(directory)
    cfg_ics = cfg.get("ics_feeds", []) or []
    cfg_rss = cfg.get("rss_feeds", []) or []
    ics = cfg_ics + dir_ics
    rss = dir_rss if dir_rss else cfg_rss
    return ics, rss


def collect(cfg: dict, ics_list: list[dict], rss_list: list[dict]) -> list[Event]:
    horizon = int(cfg.get("horizon_days", 45))
    events: list[Event] = []

    ra_cfg = cfg.get("resident_advisor", {}) or {}
    if ra_cfg.get("enabled"):
        events += _run("Resident Advisor", lambda: resident_advisor.fetch(
            area_id=int(ra_cfg.get("area_id", 34)), horizon_days=horizon))

    events += _run("iCal feeds", lambda: ics_feeds.fetch(ics_list, horizon_days=horizon))
    events += _run("RSS feeds", lambda: rss_feeds.fetch(rss_list, horizon_days=horizon))

    if (cfg.get("html_scrapers", {}) or {}).get("enabled"):
        events += _run("HTML scrapers", lambda: html_scrapers.fetch(horizon_days=horizon))

    return events


def _run(label: str, fn) -> list[Event]:
    try:
        got = fn()
        print(f"  ✓ {label}: {len(got)} events")
        return got
    except Exception as exc:
        print(f"  ! {label} skipped ({type(exc).__name__}: {exc})")
        return []


def _expected_sources(cfg: dict, ics_list: list[dict], rss_list: list[dict]) -> list[str]:
    """All source labels tried this run — so the page can show 0-count feeds."""
    labels = []
    if (cfg.get("resident_advisor", {}) or {}).get("enabled"):
        labels.append("Resident Advisor")
    for f in ics_list:
        labels.append(f"cal:{f.get('name', 'calendar')}")
    for f in rss_list:
        labels.append(f"rss:{f.get('name', 'feed')}")
    return labels


def _within_budget(e, max_price: float) -> bool:
    """Keep free / cheap / unknown-price events; drop >max_price and known-paid-no-amount."""
    if e.price_value is not None:
        return e.price_value <= max_price
    if e.is_free is True:
        return True
    if e.is_free is False:
        return False
    return True


def _upcoming(e, now, window_days: int) -> bool:
    """Keep an event only if it is really happening soon.

    One-off events must fall in [now - grace, now + window]; this drops the huge
    tail of RSS *publish dates* from old blog posts. Genuine recurring standing
    offers (an iCal repeat rule, or text like "every Wednesday") are kept
    regardless of their often-arbitrary post date, since they show dateless in
    the regulars section."""
    if e.recurring:
        return True
    try:
        dt = datetime.fromisoformat(e.start)
    except (ValueError, TypeError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - timedelta(hours=18)) <= dt <= (now + timedelta(days=window_days))


def main() -> int:
    print("Collecting Berlin events…")
    cfg = load_config()
    directory = directory_feed.load_directory()
    max_price = float(cfg.get("max_price", 5))

    ics_list, rss_list = resolve_feeds(cfg, directory)
    events = collect(cfg, ics_list, rss_list)
    events = dedupe(events)
    events = translate_events(events, enabled=cfg.get("translate", True))
    events = [e for e in events if e.start]
    before_geo = len(events)
    events = [e for e in events if berlin_status(e.venue, e.title, e.area) != "other"]
    non_berlin = before_geo - len(events)
    now = datetime.now(timezone.utc)
    window_days = int(cfg.get("horizon_days", 60))
    before_win = len(events)
    events = [e for e in events if _upcoming(e, now, window_days)]
    stale = before_win - len(events)
    # Resolve unknown-price events by reading their page, then keep only events we
    # can confirm are free or cheap — no "check price" left in the list.
    pstats = price_probe.resolve_prices(events) if cfg.get("price_probe", True) else {}
    before = len(events)
    events = [e for e in events if _within_budget(e, max_price)]      # drops >max / ticketed-no-price
    events = [e for e in events if e.is_free is not None]             # drops still-unknown price
    dropped = before - len(events)
    events.sort(key=lambda e: e.start)

    counts = Counter(e.source for e in events)
    sources = {label: counts.get(label, 0)
               for label in _expected_sources(cfg, ics_list, rss_list)}

    always_free = directory_feed.build_always_free(directory)
    manual_check = directory_feed.build_manual_check(directory)

    n_rec = sum(1 for e in events if e.recurring)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(events),
        "counts": {
            "events": len(events),
            "recurring": n_rec,
            "oneoff": len(events) - n_rec,
            "always_free": len(always_free),
            "manual_check": len(manual_check),
        },
        "sources": sources,
        "events": [e.to_dict() for e in events],
        "always_free": always_free,
        "manual_check": manual_check,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Wrote {len(events)} events ({n_rec} recurring, {len(events)-n_rec} one-off), "
          f"{len(always_free)} always-free, {len(manual_check)} manual-check "
          f"→ {OUT.relative_to(ROOT)}  [dropped {dropped} paid/unknown, {non_berlin} non-Berlin, {stale} past/undated; price-probe: {pstats.get('freed',0)} freed / {pstats.get('ticketed',0)+pstats.get('unresolved',0)} dropped of {pstats.get('checked',0)}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
