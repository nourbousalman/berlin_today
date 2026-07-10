"""Translate German event text to English.

Uses deep-translator's free Google endpoint (no API key). Runs in the GitHub
Action, which has internet access. Everything is wrapped so that if translation
is unavailable or rate-limited, events simply keep their original text. A
persistent cache (docs/.translation_cache.json) avoids re-translating the same
string and keeps request volume low.
"""
from __future__ import annotations
import json
import re
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


def translate_events(events: list, enabled: bool = True) -> list:
    if not enabled:
        return events
    cache = _load_cache()
    try:
        from deep_translator import GoogleTranslator
        translator = GoogleTranslator(source="auto", target="en")
    except Exception as exc:
        print(f"  ! translation unavailable ({exc}); leaving text as-is")
        return events

    new = 0
    for ev in events:
        touched = False
        for attr in ("title", "description"):
            val = getattr(ev, attr, None)
            if not val or not _looks_german(val):
                continue
            if val in cache:
                setattr(ev, attr, cache[val]); touched = True; continue
            try:
                out = translator.translate(val[:4900]) or val
                cache[val] = out
                setattr(ev, attr, out); touched = True; new += 1
            except Exception:
                pass          # keep original; a later run can retry
        if touched:
            ev.translated = True

    _save_cache(cache)
    print(f"  ✓ translation: {new} new string(s) translated to English")
    return events
