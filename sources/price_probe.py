"""Resolve unknown-price events by reading their own event page.

Events that arrive with is_free=None (the feed gave no price signal) get their
page fetched and scanned for free / donation / price / ticketed wording. The
aggregator then keeps only events it can confirm are free or cheap — so the
final list never shows "check price".

Results are cached by URL (docs/.price_cache.json) and the cache is committed by
the workflow, so pages aren't re-fetched every run.
"""
from __future__ import annotations
import json
import re
import ssl
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import urllib.request

from .base import looks_free, looks_donation, detect_price

_CACHE = Path(__file__).resolve().parent.parent / "docs" / ".price_cache.json"
_TICKET = re.compile(
    r"\b(tickets?|vorverkauf|vvk\b|abendkasse|karten|admission|eintrittskarten|"
    r"eintritt:? ?\d|ausverkauft|sold out)\b", re.I)
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


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


def _fetch_text(url: str) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; berlin-events/1.0)"})
    html = urllib.request.urlopen(req, timeout=12, context=_CTX).read().decode("utf-8", "ignore")
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))[:200000]


def _classify(text: str):
    """Return {is_free, price, price_value} or None if the page gives no signal."""
    if looks_free(text):
        return {"is_free": True, "price": None, "price_value": None}
    if looks_donation(text):
        return {"is_free": True, "price": "Donation", "price_value": None}
    disp, val = detect_price(text)
    if val is not None:
        return {"is_free": False, "price": disp, "price_value": val}
    if _TICKET.search(text):
        return {"is_free": False, "price": None, "price_value": None}   # ticketed, price unknown
    return None                                                          # unresolved


def resolve_prices(events: list, max_workers: int = 16) -> dict:
    """Fetch pages for is_free=None events and fill in what the page reveals.
    Mutates events in place; returns stats."""
    targets = [e for e in events if e.is_free is None and (e.url or "").startswith("http")]
    stats = {"checked": len(targets), "freed": 0, "priced": 0, "ticketed": 0, "unresolved": 0}
    if not targets:
        return stats

    cache = _load()
    todo = list({e.url for e in targets if e.url not in cache})

    def work(url):
        try:
            return url, _classify(_fetch_text(url))
        except Exception:
            return url, None

    if todo:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for url, res in ex.map(work, todo):
                cache[url] = res            # cache misses (None) too, so we don't refetch
    _save(cache)

    for e in targets:
        res = cache.get(e.url)
        if not res:
            stats["unresolved"] += 1
            continue
        e.is_free = res["is_free"]
        if res.get("price"):
            e.price = res["price"]
        if res.get("price_value") is not None:
            e.price_value = res["price_value"]
        if res["is_free"] is True:
            stats["freed"] += 1
        elif res.get("price_value") is not None:
            stats["priced"] += 1
        else:
            stats["ticketed"] += 1
    return stats
