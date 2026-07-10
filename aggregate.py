#!/usr/bin/env python3
"""Collect Berlin events from every configured source into docs/events.json.

Design: each source runs inside its own try/except, so one broken source (a
changed website, a dead feed) logs a warning and the rest still produce a full
feed. Run locally with `python aggregate.py`; in production it runs on a
schedule via .github/workflows/update.yml.
"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import json
import sys
from collections import Counter
import yaml

from sources.base import Event, dedupe
from sources import resident_advisor, ics_feeds, rss_feeds, html_scrapers
from sources.translate import translate_events

ROOT = Path(__file__).parent
OUT = ROOT / "docs" / "events.json"


def load_config() -> dict:
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def collect(cfg: dict) -> list[Event]:
    horizon = int(cfg.get("horizon_days", 45))
    events: list[Event] = []

    ra_cfg = cfg.get("resident_advisor", {}) or {}
    if ra_cfg.get("enabled"):
        events += _run("Resident Advisor", lambda: resident_advisor.fetch(
            area_id=int(ra_cfg.get("area_id", 34)), horizon_days=horizon))

    events += _run("iCal feeds", lambda: ics_feeds.fetch(
        cfg.get("ics_feeds", []), horizon_days=horizon))

    events += _run("RSS feeds", lambda: rss_feeds.fetch(
        cfg.get("rss_feeds", []), horizon_days=horizon))

    if (cfg.get("html_scrapers", {}) or {}).get("enabled"):
        events += _run("HTML scrapers", lambda: html_scrapers.fetch(horizon_days=horizon))

    return events


def _run(label: str, fn) -> list[Event]:
    """Run one source, swallowing failures so the overall run always completes."""
    try:
        got = fn()
        print(f"  ✓ {label}: {len(got)} events")
        return got
    except Exception as exc:
        print(f"  ! {label} skipped ({type(exc).__name__}: {exc})")
        return []


def _expected_sources(cfg: dict) -> list[str]:
    """All source labels we tried this run — so the page can show 0-count feeds."""
    labels = []
    if (cfg.get("resident_advisor", {}) or {}).get("enabled"):
        labels.append("Resident Advisor")
    for f in cfg.get("ics_feeds", []) or []:
        labels.append(f"cal:{f.get('name', 'calendar')}")
    for f in cfg.get("rss_feeds", []) or []:
        labels.append(f"rss:{f.get('name', 'feed')}")
    return labels


def main() -> int:
    print("Collecting Berlin events…")
    cfg = load_config()
    events = collect(cfg)
    events = dedupe(events)
    events = translate_events(events, enabled=cfg.get("translate", True))
    events = [e for e in events if e.start]                       # drop dateless
    events.sort(key=lambda e: e.start)

    counts = Counter(e.source for e in events)
    sources = {label: counts.get(label, 0) for label in _expected_sources(cfg)}

    OUT.parent.mkdir(parents=True, exist_ok=True)
    n_rec = sum(1 for e in events if e.recurring)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(events),
        "sources": sources,
        "events": [e.to_dict() for e in events],
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Wrote {len(events)} events ({n_rec} recurring, {len(events)-n_rec} one-off) → {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
