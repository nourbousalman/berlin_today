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


def translate_events(events: list, enabled: bool = True, max_workers: int = 8) -> list:
    if not enabled:
        return events
    cache = _load_cache()
    try:
        from deep_translator import GoogleTranslator
        GoogleTranslator(source="auto", target="en")  # probe availability early
    except Exception as exc:
        print(f"  ! translation unavailable ({exc}); leaving text as-is")
        return events

    # 1) Collect the (event, attr, value) slots that are German, and the set of
    #    unique values still missing from the cache.
    slots: list[tuple] = []
    todo: set[str] = set()
    for ev in events:
        for attr in ("title", "description"):
            val = getattr(ev, attr, None)
            if not val or not _looks_german(val):
                continue
            slots.append((ev, attr, val))
            if val not in cache:
                todo.add(val)

    # 2) Translate the unique missing strings concurrently.
    def _tr(s: str):
        try:
            return s, (GoogleTranslator(source="auto", target="en").translate(s[:4900]) or s)
        except Exception:
            return s, None

    new = 0
    if todo:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for s, out in ex.map(_tr, list(todo)):
                if out is not None:
                    cache[s] = out
                    new += 1

    # 3) Apply cached translations back onto the events.
    for ev, attr, val in slots:
        if val in cache:
            setattr(ev, attr, cache[val])
            ev.translated = True

    _save_cache(cache)
    print(f"  ✓ translation: {new} new string(s) translated "
          f"({len(slots)} German slots, {len(cache)} cached total)")
    return events
