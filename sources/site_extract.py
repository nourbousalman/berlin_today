"""Pull events from sites that have NO feed (the old "manual check" list).

Three tiers, cheapest first:

  1. Page discovery (free) — fetch the homepage, follow the most promising
     programme/calendar link, keep the most date-dense page.
  2. Schema.org JSON-LD (free) — many sites embed machine-readable Event objects
     even when they publish no feed. Parsed directly, no LLM needed.
  3. LLM extraction — for the majority whose events live in plain HTML with a
     different layout on every site. The page text is handed to a small model
     which returns structured JSON.

Cost control:
  • Every page is hashed; if it hasn't changed since last run, the cached events
    are reused and no model call is made.
  • `llm_budget_per_run` caps model calls per run. Sites are processed
    least-recently-checked first, so the whole directory is covered in rotation.

Inference backend (auto-detected, in order):
  • GITHUB_TOKEN  → GitHub Models, free inside GitHub Actions. Requires
    `permissions: models: read` in the workflow. No API key to manage.
  • ANTHROPIC_API_KEY → used instead if present (no per-day cap, larger context).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .base import Event, normalise_category, looks_free, looks_donation, detect_price
from .verify import is_junk

_CACHE = Path(__file__).resolve().parent.parent / "docs" / ".extract_cache.json"
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_UA = {"User-Agent": "Mozilla/5.0 (compatible; berlin-events/1.0)"}

_GH_ENDPOINT = "https://models.github.ai/inference/chat/completions"
_ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"

_LD = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.I | re.S)
_EVENT_LINK = re.compile(
    r'href=["\']([^"\']*(?:programm|veranstaltung|kalender|termine|events?|calendar|agenda|spielplan)[^"\']*)["\']',
    re.I)
_MONTHS = "januar|februar|märz|maerz|april|mai|juni|juli|august|september|oktober|november|dezember"
_DATE_HINT = re.compile(r'\b[0-3]?\d\.([01]?\d|' + _MONTHS + r')\.?\s*(20\d\d)?\b', re.I)
_EVENTISH = re.compile(
    r'(veranstaltung|programm|konzert|ausstellung|workshop|lesung|führung|termin|event|vortrag|film)', re.I)


# --------------------------------------------------------------------------- cache
def _load_cache() -> dict:
    try:
        return json.loads(_CACHE.read_text("utf-8"))
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    try:
        _CACHE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE.write_text(json.dumps(cache, ensure_ascii=False), "utf-8")
    except Exception:
        pass


# --------------------------------------------------------------------------- fetching
def _get(url: str, timeout: int = 8) -> str:
    try:
        req = urllib.request.Request(url, headers=_UA)
        return urllib.request.urlopen(req, timeout=timeout, context=_CTX).read().decode("utf-8", "ignore")
    except Exception:
        return ""


def _to_text(html: str) -> str:
    html = re.sub(r"<(script|style|nav|footer)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _score(text: str) -> int:
    """How much this page looks like a real event listing."""
    if not _EVENTISH.search(text):
        return 0
    return len(_DATE_HINT.findall(text))


def find_event_page(base_url: str) -> tuple[str, str, str]:
    """Return (best_url, html, text) for the most event-like page on a site."""
    base = base_url.rstrip("/")
    home = _get(base)
    if not home:
        return "", "", ""
    best = (base, home, _to_text(home))
    best_score = _score(best[2])

    candidates: list[str] = []
    for m in _EVENT_LINK.finditer(home):
        href = m.group(1)
        if href.startswith("#") or "mailto:" in href:
            continue
        url = href if href.startswith("http") else base + ("" if href.startswith("/") else "/") + href.lstrip("/")
        if url.rstrip("/") != base and url not in candidates:
            candidates.append(url)
        if len(candidates) >= 3:
            break
    for url in candidates:
        html = _get(url, 7)
        if not html:
            continue
        text = _to_text(html)
        sc = _score(text)
        if sc > best_score:
            best, best_score = (url, html, text), sc
    return best


# --------------------------------------------------------------------------- tier 2: JSON-LD
def _iter_jsonld(html: str):
    for m in _LD.finditer(html):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except Exception:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if "@graph" in node:
                    stack.append(node["@graph"])
                yield node


def parse_jsonld(html: str) -> list[dict]:
    """Extract Schema.org Event objects — free, no model call."""
    out = []
    for node in _iter_jsonld(html):
        types = node.get("@type") or ""
        types = types if isinstance(types, list) else [types]
        if not any("Event" in str(t) for t in types):
            continue
        name = node.get("name")
        start = node.get("startDate")
        if not name or not start:
            continue
        offers = node.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        price = offers.get("price") if isinstance(offers, dict) else None
        try:
            pval = float(str(price).replace(",", ".")) if price not in (None, "") else None
        except Exception:
            pval = None
        loc = node.get("location") or {}
        if isinstance(loc, list):
            loc = loc[0] if loc else {}
        venue = loc.get("name") if isinstance(loc, dict) else None
        addr = loc.get("address") if isinstance(loc, dict) else None
        if isinstance(addr, dict):
            venue = " ".join(filter(None, [venue, addr.get("streetAddress"),
                                           addr.get("postalCode"), addr.get("addressLocality")]))
        elif isinstance(addr, str):
            venue = " ".join(filter(None, [venue, addr]))
        out.append({
            "date_source": "jsonld",
            "title": str(name)[:200],
            "start": str(start),
            "venue": venue,
            "price_value": pval,
            "is_free": True if pval == 0 else (False if pval else None),
            "url": node.get("url"),
        })
    return out


# --------------------------------------------------------------------------- tier 3: LLM
_PROMPT = """You extract events from a venue's web page.

Return ONLY a JSON array (no prose, no markdown fences). One object per event:
{{"title": str, "date": "YYYY-MM-DD", "time": "HH:MM" or null,
  "price_eur": number or null, "free": true/false/null, "venue": str or null}}

Rules:
- Only real, individual, dated events happening on or after {today}.
- Ignore navigation, opening hours, news articles, exhibitions with no specific date,
  job ads, newsletters and anything that is not a scheduled event.
- "free"=true only if the page says it is free / kostenlos / Eintritt frei / donation.
  If a price is stated, set price_eur. If neither is stated, use null for both.
- If the year is missing, infer the nearest future year.
- Max 40 events. If there are no real dated events, return [].

VENUE: {venue}
PAGE TEXT:
{text}"""


def _post_json(url: str, payload: dict, headers: dict, timeout: int = 90) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def _llm_call(prompt: str, model: str) -> str:
    """Call GitHub Models (free in Actions) or Anthropic, whichever is configured."""
    anth = os.environ.get("ANTHROPIC_API_KEY")
    if anth:
        data = _post_json(_ANTHROPIC_ENDPOINT, {
            "model": os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
            "max_tokens": 4000,
            "messages": [{"role": "user", "content": prompt}],
        }, {"x-api-key": anth, "anthropic-version": "2023-06-01"})
        return "".join(b.get("text", "") for b in data.get("content", []))

    gh = os.environ.get("GITHUB_TOKEN")
    if gh:
        data = _post_json(_GH_ENDPOINT, {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 4000,
        }, {"Authorization": f"Bearer {gh}", "Accept": "application/vnd.github+json"})
        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "")

    raise RuntimeError("no inference backend (set GITHUB_TOKEN with models:read, or ANTHROPIC_API_KEY)")


def _parse_llm_json(raw: str) -> list[dict]:
    raw = (raw or "").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        data = json.loads(raw[start:end + 1])
    except Exception:
        return []
    return [d for d in data if isinstance(d, dict)]


def llm_extract(text: str, venue: str, model: str, char_limit: int = 22000) -> list[dict]:
    """Ask the model for structured events. Text is truncated to respect the
    8K-token input cap on GitHub Models' free tier."""
    prompt = _PROMPT.format(today=datetime.now(timezone.utc).date().isoformat(),
                            venue=venue, text=text[:char_limit])
    return _parse_llm_json(_llm_call(prompt, model))


# --------------------------------------------------------------------------- assembly
def _mk_event(d: dict, name: str, url: str, category: str, default_free, page_text: str) -> Event | None:
    title = (d.get("title") or "").strip()
    if not title or is_junk(title):
        return None
    start = d.get("start") or d.get("date")
    if not start:
        return None
    start = str(start)
    if len(start) == 10:                                  # date only -> add a time
        start += "T" + (d.get("time") or "00:00") + ":00+02:00"
    try:
        datetime.fromisoformat(start.replace("Z", "+00:00"))
    except Exception:
        return None

    free = d.get("free", d.get("is_free"))
    pval = d.get("price_eur", d.get("price_value"))
    try:
        pval = float(pval) if pval is not None else None
    except Exception:
        pval = None
    if pval == 0:
        free, pval = True, None
    if free is None and pval is None:                      # fall back to page wording / source default
        if looks_free(title) or looks_donation(title):
            free = True
        else:
            free = default_free
    return Event(
        date_source=d.get("date_source", "llm"),
        title=title[:200],
        start=start,
        source=f"web:{name}",
        url=d.get("url") or url,
        venue=(d.get("venue") or None),
        category=category,
        is_free=(bool(free) if free is not None else None),
        price=(f"€{pval:g}" if pval is not None else None),
        price_value=pval,
        description=None,
        recurring=False,
    )


def fetch(directory: list[dict], cfg: dict, group_category, group_free) -> list[Event]:
    """Extract events from every feedless directory entry, within budget."""
    sites = [e for e in directory
             if not (e.get("rss") or e.get("ical"))
             and (e.get("url") or "").startswith("http")
             and (e.get("status") or "").lower() not in ("parked", "dead")]
    if not sites:
        return []

    cache = _load_cache()
    budget = int(cfg.get("llm_budget_per_run", 40))
    model = cfg.get("llm_model", "openai/gpt-4o-mini")
    have_backend = bool(os.environ.get("GITHUB_TOKEN") or os.environ.get("ANTHROPIC_API_KEY"))

    # least-recently-checked first, so the directory is covered in rotation
    sites.sort(key=lambda e: cache.get(str(e.get("id", e["name"])), {}).get("checked", ""))

    events: list[Event] = []
    used = 0
    stats = {"sites": 0, "jsonld": 0, "llm": 0, "cached": 0, "empty": 0}

    def load_page(e):
        return e, find_event_page(e["url"])

    with ThreadPoolExecutor(max_workers=10) as ex:
        pages = list(ex.map(load_page, sites))

    for e, (page_url, html, text) in pages:
        key = str(e.get("id", e["name"]))
        name = e["name"]
        cat = group_category(e.get("group", ""))
        default_free = e.get("free", group_free(e.get("group", "")))
        if not text:
            continue
        stats["sites"] += 1
        digest = hashlib.sha1(text[:60000].encode("utf-8")).hexdigest()
        entry = cache.get(key) or {}

        raw: list[dict] | None = None
        if entry.get("hash") == digest and entry.get("events") is not None:
            raw = entry["events"]                          # unchanged page -> reuse
            stats["cached"] += 1
        else:
            found = parse_jsonld(html)                     # free tier first
            if found:
                raw = found
                stats["jsonld"] += 1
            elif have_backend and used < budget and _score(text) >= 2:
                try:
                    raw = llm_extract(text, name, model)
                    used += 1
                    stats["llm"] += 1
                    time.sleep(0.4)                        # be gentle with rate limits
                except Exception as exc:
                    print(f"  ! extract '{name}': {exc}")
                    raw = None
            if raw is not None:
                cache[key] = {"hash": digest, "events": raw,
                              "checked": datetime.now(timezone.utc).isoformat()}

        if not raw:
            stats["empty"] += 1
            continue
        for d in raw:
            ev = _mk_event(d, name, page_url or e["url"], cat, default_free, text)
            if ev:
                events.append(ev)

    _save_cache(cache)
    print(f"  ✓ site extraction: {len(events)} events from {stats['sites']} sites "
          f"(jsonld {stats['jsonld']}, llm {stats['llm']}/{budget}, cached {stats['cached']})")
    return events
