"""Find a description for events whose source gave none, from their own page.

Feeds often carry only a title ("Adenauer auf der Wolke", "Sprachcafé"). The
labeller can only explain what it is told, so for every event that arrives
without a description we read its own page once and keep the page's own summary:
schema.org Event description, og:description / meta description, or the first
substantial paragraphs. Results are cached by URL (docs/.desc_cache.json) and
committed, so a page is read once, not every run. Polite: two connections, a
pause, a per-run budget, and gratis-in-berlin is left to its own reader.
"""
from __future__ import annotations

import html as _html
import json
import re
import ssl
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

_CACHE = Path(__file__).resolve().parent.parent / "docs" / ".desc_cache.json"
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_UA = {"User-Agent": "Mozilla/5.0 (compatible; berlin-events/1.0; free-events list)"}
_SKIP_HOSTS = ("gratis-in-berlin.de", "instagram.com", "facebook.com", "stressfaktor")


def _load() -> dict:
    try:
        return json.loads(_CACHE.read_text("utf-8"))
    except Exception:
        return {}


def _clean(s: str) -> str:
    s = _html.unescape(s or "")                 # escaped markup inside JSON: &lt;p&gt;
    s = _html.unescape(re.sub(r"<[^>]+>", " ", s)).replace("\\n", " ")
    return re.sub(r"\s+", " ", s).strip()


def _jsonld_desc(page: str) -> str:
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, dict):
                if "@graph" in d:
                    stack.extend(d["@graph"] if isinstance(d["@graph"], list) else [d["@graph"]])
                t = d.get("@type")
                t = " ".join(t) if isinstance(t, list) else str(t or "")
                if "Event" in t and d.get("description"):
                    return _clean(str(d["description"]))
    return ""


def _meta(page: str, name: str) -> str:
    m = re.search(r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]*content=["\']([^"\']{20,})["\']' % re.escape(name),
                  page, re.I) or \
        re.search(r'<meta[^>]+content=["\']([^"\']{20,})["\'][^>]*(?:property|name)=["\']%s["\']' % re.escape(name),
                  page, re.I)
    return _clean(m.group(1)) if m else ""


def _paragraphs(page: str) -> str:
    body = re.sub(r"<(script|style|nav|header|footer|form)[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)
    ps = [_clean(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", body, re.S | re.I)]
    ps = [p for p in ps if len(p) > 60 and not re.search(r"cookie|datenschutz|javascript|newsletter", p, re.I)]
    return " ".join(ps[:3])


def _describe(url: str) -> str | None:
    """Text describing the page, '' if it has none, None if it could not be read."""
    try:
        req = urllib.request.Request(url, headers=_UA)
        page = urllib.request.urlopen(req, timeout=15, context=_CTX).read(600_000).decode("utf-8", "ignore")
    except Exception:
        return None
    if not urlparse(url).path.strip("/"):
        # a front page lists many events: its first event is not this one
        tries = (_meta(page, "og:description"), _meta(page, "description"))
    else:
        tries = (_jsonld_desc(page), _meta(page, "og:description"), _meta(page, "description"),
                 _paragraphs(page))
    for txt in tries:
        if txt and len(txt) >= 40:
            return txt[:900]
    return ""


def fill(events: list, budget: int = 80, max_workers: int = 2) -> dict:
    """Give events without any description the text of their own page."""
    cache = _load()
    need = [e for e in events
            if not getattr(e, "detail_text", None)
            and (e.url or "").startswith("http")
            and not any(h in (e.url or "") for h in _SKIP_HOSTS)]
    todo = []
    for u in dict.fromkeys(e.url for e in need):
        if u not in cache and len(todo) < budget:
            todo.append(u)

    def work(u):
        res = _describe(u)
        time.sleep(0.5)
        return u, res

    if todo:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for u, res in ex.map(work, todo):
                if res is not None:          # unreadable pages are retried next run
                    cache[u] = res
        try:
            _CACHE.write_text(json.dumps(cache, ensure_ascii=False), "utf-8")
        except Exception:
            pass

    filled = 0
    for e in need:
        txt = cache.get(e.url)
        if txt:
            path = urlparse(e.url).path.strip("/")
            # a site's front page describes the place, not this event: say so
            e.detail_text = (f"(from the organiser's website) {txt}" if not path else txt)
            filled += 1
    print(f"  ✓ descriptions from event pages: {filled} of {len(need)} events without one "
          f"({len(todo)} pages read this run, {len(cache)} cached)")
    return {"filled": filled, "need": len(need)}
