"""Translate German event text to English.

Uses deep-translator's free Google endpoint (no API key). Runs in the GitHub
Action, which has internet access. Everything is wrapped so that if translation
is unavailable or rate-limited, events simply keep their original text.

Speed:
  • English text is skipped (langdetect / umlaut / German-word heuristics).
  • Identical strings are translated once (deduped).
  • Remaining strings are translated concurrently (thread pool).
  • A persistent cache (docs/.translation_cache.json) is reused across runs —
    but only if the workflow commits it. With the cache committed, steady-state
    runs translate only the handful of *new* strings, so they finish in seconds.
"""
from __future__ import annotations
import json
import time
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_CACHE_PATH = Path(__file__).resolve().parent.parent / "docs" / ".translation_cache.json"
_UMLAUT = re.compile(r"[äöüßÄÖÜ]")
_DE_WORDS = re.compile(
    r"\b(und|oder|für|mit|der|die|das|ein|eine|kein|nicht|veranstaltung|kostenlos|"
    r"jeden|jede|uhr|einlass|eintritt|kinder|gespräch|führung|ausstellung|nur|sich|"
    r"wir|ist|sind|im|am|zum|zur|auf|von|bei|über|treffen|anmeldung)\b", re.I)


def _looks_german(text: str) -> bool:
    if not text or len(text) < 3:
        return False
    if _UMLAUT.search(text) or _DE_WORDS.search(text):
        return True
    try:
        from langdetect import detect
        return detect(text) == "de"
    except Exception:
        return False


def _load_cache() -> dict:
    try:
        return json.loads(_CACHE_PATH.read_text("utf-8"))
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=0), "utf-8")
    except Exception:
        pass


def translate_events(events: list, enabled: bool = True, max_workers: int = 2,
                     budget: int = 400) -> list:
    """Fallback translation for titles the classifier has not rewritten yet.

    The free Google endpoint allows 5 requests per second. The old code ran 8 at
    once, so almost every request was refused — silently, because errors were
    swallowed. Now: few workers, a short pause, two retries with back-off, a
    per-run budget (the cache carries the rest over to the next run), and the
    number of failures is printed instead of hidden.
    """
    if not enabled:
        return events
    cache = _load_cache()
    try:
        from deep_translator import GoogleTranslator
        GoogleTranslator(source="auto", target="en")
    except Exception as exc:
        print(f"  ! translation unavailable ({exc}); leaving text as-is")
        return events

    slots: list[tuple] = []
    todo: list[str] = []
    seen: set[str] = set()
    for ev in events:
        if getattr(ev, "translated", False):        # classifier already gave English
            continue
        for attr in ("title", "description"):
            val = getattr(ev, attr, None)
            if not val or not _looks_german(val):
                continue
            slots.append((ev, attr, val))
            if val not in cache and val not in seen:
                seen.add(val)
                todo.append(val)
    deferred = max(0, len(todo) - budget)
    todo = todo[:budget]

    def _tr(s: str):
        for attempt in range(3):
            try:
                out = GoogleTranslator(source="auto", target="en").translate(s[:4900])
                time.sleep(0.5)          # Google allows 5 requests/second
                return s, (out or s)
            except Exception:
                time.sleep(1.5 * (attempt + 1))
        return s, None

    new = failed = 0
    if todo:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for s, out in ex.map(_tr, todo):
                if out is None:
                    failed += 1
                else:
                    cache[s] = out
                    new += 1

    for ev, attr, val in slots:
        if val in cache:
            if attr == "title" and not getattr(ev, "title_orig", None):
                ev.title_orig = val
            setattr(ev, attr, cache[val])
            ev.translated = True

    _save_cache(cache)
    print(f"  ✓ translation: {new} new, {failed} failed, {deferred} left for next run "
          f"({len(cache)} cached total)")
    if failed and not new:
        print("  !! every translation failed this run — the free endpoint is probably blocking us")
    return events
