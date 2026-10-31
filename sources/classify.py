"""Make every event understandable: English title, what kind of thing it is,
who it is for, and one plain line on what actually happens.

Why: listing titles are written for insiders ("Tanja Ostojić: Encounters — On
Bodies, Borders, Resistance", "KinderKulturMonat – Märchenlauschen"). A reader
cannot tell an exhibition from a counselling session, or a kids' workshop from
an adult one. This module answers four questions per event:

    title     clear English title (names of people/works/venues kept as-is)
    kind      exhibition | music | talk | film | theatre | workshop | tour |
              party | market | festival | sport | meetup | support | notice
    audience  everyone | kids | seniors | group | women | men
    summary   one short plain-English sentence, restating only what the
              listing says — nothing invented

"notice" means the entry is not an event at all (job ad, opening-hours notice,
call for applications); the aggregator drops those.

How:
  * Titles are sent in batches to a small language model — GitHub Models (free
    inside GitHub Actions via GITHUB_TOKEN with `permissions: models: read`), or
    the Anthropic API if ANTHROPIC_API_KEY is set.
  * Results are cached by title in docs/.classify_cache.json, so each title is
    classified once, ever. Steady-state runs only send the few new titles.
  * Anything the model has not covered yet (no backend, quota used up, error)
    gets a keyword-based best guess instead, flagged `classified_by: "rules"`,
    so the site works either way and improves as the cache fills.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

_CACHE = Path(__file__).resolve().parent.parent / "docs" / ".classify_cache.json"
_CTX = ssl.create_default_context()

KINDS = ("exhibition", "music", "talk", "film", "theatre", "workshop", "tour",
         "party", "market", "festival", "sport", "meetup", "support", "notice")
AUDIENCES = ("everyone", "kids", "seniors", "group", "women", "men")

_GH_URL = "https://models.github.ai/inference/chat/completions"
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

_PROMPT = """You label listings for a website of free events in Berlin. Readers are adults \
who want to know quickly what each thing is and whether it is for them.

For EACH numbered listing return one JSON object with these keys:
  "i":        the listing number
  "title":    a clear English title. Translate German. Keep names of people, works,
              venues and organisations exactly as written. No dates, times, prices or emoji.
              At most 70 characters.
  "kind":     exactly one of: exhibition, music, talk, film, theatre, workshop, tour,
              party, market, festival, sport, meetup, support, notice
  "audience": exactly one of: everyone, kids, seniors, group, women, men
  "summary":  one plain English sentence, at most 90 characters, saying what actually
              happens. Use ONLY information in the listing. Do not invent details,
              do not praise, do not guess at things the listing does not say.

Meaning of "kind":
  exhibition = art or museum exhibition, open studios, installation
  music      = concert, jam session, choir, DJ set that is mainly about the music
  talk       = talk, lecture, reading, panel, discussion, book presentation
  film       = film screening
  theatre    = theatre, dance performance, comedy, cabaret, opera, open mic
  workshop   = hands-on session or course, repair café, sewing café, open lab
  tour       = guided tour, city walk, public tour of a building or memorial
  party      = party, club night, rave
  market     = flea market, fair, swap meet, food market
  festival   = festival or street fair with a mixed programme
  sport      = sport, yoga, running, hiking, dance class, movement
  meetup     = social meetup, language café, game night, regulars' table, breakfast club
  support    = advice, counselling, consultation hours, self-help group,
               information session about a service
  notice     = NOT an event: job advert, call for applications, opening-hours notice,
               news item, description of a permanent service

Meaning of "audience":
  everyone = any adult member of the public would be welcome
  kids     = for children or teenagers, school classes, or families with young children
  seniors  = for older people
  group    = for one specific group rather than the general public
             (e.g. refugees, new parents, carers, a profession, members only)
  women    = women only (including women/FLINTA-only events)
  men      = men only

Return ONLY a JSON array, nothing else.

Listings:
{listings}"""


# ------------------------------------------------------------------ cache
def _load() -> dict:
    try:
        return json.loads(_CACHE.read_text("utf-8"))
    except Exception:
        return {}


def _save(cache: dict) -> None:
    try:
        _CACHE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0), "utf-8")
    except Exception:
        pass


def _key(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").strip())


# ------------------------------------------------------------------ backend
class QuotaExhausted(Exception):
    pass


def _post(url: str, payload: dict, headers: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
            body = r.read().decode("utf-8", "ignore")
        try:
            return json.loads(body)
        except ValueError:
            raise RuntimeError(f"service answered with non-JSON (HTTP {r.status}): "
                               f"{body[:160]!r}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:300]
        if e.code == 429:
            raise QuotaExhausted(f"HTTP 429 rate limit: {body}")
        raise RuntimeError(f"HTTP {e.code}: {body}")


def _call(prompt: str, models: list[str]) -> tuple[str, str]:
    """Return (response_text, model_used). Tries models in order on 'unknown model'."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
        data = _post(_ANTHROPIC_URL, {"model": model, "max_tokens": 4000,
                                      "messages": [{"role": "user", "content": prompt}]},
                     {"x-api-key": key, "anthropic-version": "2023-06-01"})
        return "".join(b.get("text", "") for b in data.get("content", [])), model

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("no backend: GITHUB_TOKEN not set (workflow needs "
                           "`permissions: models: read` and env GITHUB_TOKEN)")
    last = None
    for model in models:
        try:
            data = _post(_GH_URL, {"model": model, "temperature": 0, "max_tokens": 4000,
                                   "messages": [{"role": "user", "content": prompt}]},
                         {"Authorization": f"Bearer {token}",
                          "Accept": "application/vnd.github+json"})
            return (data.get("choices") or [{}])[0].get("message", {}).get("content", ""), model
        except QuotaExhausted:
            last = "quota"
            continue                       # each model has its own daily quota
        except RuntimeError as e:
            last = str(e)
            if re.search(r"HTTP (400|404)", last) and "model" in last.lower():
                continue                   # model id not available -> try next
            raise
    if last == "quota":
        raise QuotaExhausted("daily quota used up on all configured models")
    raise RuntimeError(last or "no model answered")


def _parse(raw: str) -> list[dict]:
    raw = re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(), flags=re.M).strip()
    a, b = raw.find("["), raw.rfind("]")
    if a == -1 or b == -1:
        return []
    try:
        data = json.loads(raw[a:b + 1])
    except Exception:
        return []
    return [d for d in data if isinstance(d, dict)]


# A summary that only admits there is nothing to say is worse than no summary.
_FILLER = re.compile(r"no (further |more )?(details|information|description)|not (specified|stated|given)|"
                     r"\bunclear\b|title only|listing (only|does not)|no more info|"
                     r"^(a |an |the )?(listing|event|session|item|entry|programme|program) "
                     r"(titled|called|named)\b|^listing\b", re.I)


def _clean(d: dict, original: str) -> dict | None:
    kind = str(d.get("kind", "")).strip().lower()
    aud = str(d.get("audience", "")).strip().lower()
    title = re.sub(r"\s+", " ", str(d.get("title") or "")).strip()
    summary = re.sub(r"\s+", " ", str(d.get("summary") or "")).strip()
    if kind not in KINDS or aud not in AUDIENCES or not title:
        return None
    if _FILLER.search(summary) or summary.rstrip(".").lower() == title.lower():
        summary = ""
    return {"title": title[:90] or original, "kind": kind, "audience": aud,
            "summary": summary[:140], "by": "ai"}


# ------------------------------------------------------------------ rules
_R = lambda p: re.compile(p, re.I)
_RULE_NOTICE = _R(r"stellenausschreibung|stellenangebot|\bbewerbung|fellowship|open call|"
                  r"ausschreibung|ab sofort|neue öffnungszeiten|geschlossen|employee for|"
                  r"wir suchen|mitarbeiter\*?in")
_RULE_AUD = [
    ("kids", _R(r"kinder|\bkids?\b|\bkita\b|famili|\bfamily|jugend|\bteens?\b|\bbaby|"
                r"kleinkind|grundschul|schulklass|\d+\s*-?\s*bis\s*\d+\s*-?jährig|ab \d+ jahren|"
                r"ferienprogramm|märchen|eltern-kind")),
    ("seniors", _R(r"senior|ältere menschen|rentner|\b[5-7]\d\s?\+|ü\s?[5-7]\d|menschen ab [5-7]\d")),
    ("men", _R(r"\bmen only\b|\(men only\)|nur für männer|\bmänner(?:gruppe|treff|abend)")),
    ("women", _R(r"\bfrauen\b|\bwomen\b|\bladies\b|flinta|frauencafé|für frauen")),
    ("group", _R(r"geflüchtet|refugee|migrant|neuzugewandert|pflegende angehörige|"
                 r"alleinerziehend|\beltern(?:café|treff|gruppe)?\b|parents|co-parenting|"
                 r"regenbogenfamili|rainbow famil|nur für mitglieder|members only")),
]
_RULE_KIND = [
    ("support", _R(r"beratung|sprechstunde|selbsthilfe|consultation|counsel+ing|advice|"
                   r"informationsabend|info-?abend|infoveranstaltung|telefonberatung|"
                   r"asylrecht|rechtsberatung")),
    ("market", _R(r"flohmar|trödel|troedel|antikmarkt|kleidertausch|tauschmarkt|basar|"
                  r"bazar|wochenmarkt|designmarkt|streetfood|food market|\bmarkt\b|market\b")),
    ("film", _R(r"\bfilm|\bkino\b|cinema|screening|matinee|\bomu\b|\bomdu\b")),
    ("talk", _R(r"lesung|\breading\b|vortrag|lecture|\btalk\b|gespräch|podium|diskussion|"
                r"buchvorstellung|buchpremiere|literatur|shared reading|lesekreis|lesebühne|"
                r"poetry|spoken word|slam")),
    ("theatre", _R(r"theater|theatre|comedy|kabarett|cabaret|stand.?up|open mic|\boper\b|"
                   r"opera|tanzperformance|performance|improv")),
    ("tour", _R(r"führung|\btour\b|rundgang|spaziergang|stadtführung|\bwalk\b|denkmal")),
    ("exhibition", _R(r"ausstellung|exhibition|vernissage|finissage|galerie|gallery|"
                      r"open studios?|offene ateliers|installation|malerei|fotografie|"
                      r"\((mo|di|mi|do|fr|sa|so)\s*[-–]\s*(mo|di|mi|do|fr|sa|so)\)|\(täglich\)")),
    ("music", _R(r"konzert|concert|\bjazz|\bchor\b|choir|\bband\b|\bdj\b|musik|music|"
                 r"jam session|jamsession|orgel|klavier|piano|singen|\bsongs?\b|liedermacher|"
                 r"karaoke")),
    ("party", _R(r"\bparty|\brave\b|clubnacht|club night|disco|tanzabend")),
    ("festival", _R(r"festival|sommerfest|straßenfest|hoffest|kiezfest|\bfest\b")),
    ("workshop", _R(r"workshop|\bkurs\b|course|repair|reparatur|nähcafé|sewing|upcycling|"
                    r"werkstatt|\blab\b|basteln|druck|batik|tüftel|kreation|nähen|stricken")),
    ("sport", _R(r"yoga|qi ?gong|tai chi|fitness|lauf|\brun\b|running|wander|hike|sport|"
                 r"bewegung|schwimm|radtour|\brad\b|klettern|tanzkurs|dance class|pilates|"
                 r"meditation|forró|tango|salsa")),
    ("meetup", _R(r"sprachcaf|sprach-caf|language caf|\bsprachtreff|stammtisch|spieleabend|"
                  r"spieletreff|board ?gam|brettspiel|\btreff|meetup|meet up|frühstück|"
                  r"breakfast|social|quiz|küfa|café|cafe")),
]
_OLD_CAT = {"music": "music", "nightlife": "party", "art": "exhibition", "sport": "sport",
            "market": "market"}


def rules(title: str, old_category: str | None = None) -> dict:
    """Keyword best guess — used only until the model has seen this title."""
    t = title or ""
    aud = next((a for a, rx in _RULE_AUD if rx.search(t)), "everyone")
    if _RULE_NOTICE.search(t):
        kind = "notice"
    else:
        kind = next((k for k, rx in _RULE_KIND if rx.search(t)), None) \
            or _OLD_CAT.get(old_category or "", "misc")
    return {"title": None, "kind": kind, "audience": aud, "summary": None, "by": "rules"}


# ------------------------------------------------------------------ main
def classify_events(events: list, cfg: dict) -> dict:
    """Annotate events in place. Returns stats for the run log."""
    cache = _load()
    batch_size = int(cfg.get("classify_batch", 30))
    budget = int(cfg.get("classify_calls_per_run", 20))
    models = cfg.get("classify_models") or ["openai/gpt-4.1-mini", "openai/gpt-4o-mini"]
    enabled = cfg.get("classify", True)

    todo, seen = [], set()
    for e in events:
        k = _key(e.title)
        if k and k not in cache and k not in seen:
            seen.add(k)
            todo.append((k, (e.venue or "")[:60]))

    stats = {"cached": 0, "new_ai": 0, "rules": 0, "calls": 0, "model": None, "error": None}
    if enabled and todo:
        for start in range(0, len(todo), batch_size):
            if stats["calls"] >= budget:
                break
            chunk = todo[start:start + batch_size]
            listing = "\n".join(f"{i + 1}. {t}" + (f"  | venue: {v}" if v else "")
                                for i, (t, v) in enumerate(chunk))
            try:
                raw, model = _call(_PROMPT.format(listings=listing), models)
                stats["calls"] += 1
                stats["model"] = model
            except QuotaExhausted as exc:
                stats["error"] = str(exc)
                break
            except Exception as exc:
                stats["error"] = str(exc)
                break                      # do not hammer a failing backend
            for d in _parse(raw):
                try:
                    idx = int(d.get("i")) - 1
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < len(chunk):
                    title = chunk[idx][0]
                    clean = _clean(d, title)
                    if clean:
                        cache[title] = clean
                        stats["new_ai"] += 1
            time.sleep(0.5)
        _save(cache)

    for e in events:
        k = _key(e.title)
        res = cache.get(k)
        if res:
            stats["cached"] += 1 if res.get("by") == "ai" else 0
        else:
            res = rules(e.title, getattr(e, "category", None))
            stats["rules"] += 1
        e.kind = res["kind"]
        e.audience = res["audience"]
        e.summary = res.get("summary")
        e.classified_by = res.get("by", "rules")
        if res.get("title") and res["title"].strip().lower() != k.lower():
            e.title_orig = e.title
            e.title = res["title"]
            e.translated = True
        e.category = e.kind                # one taxonomy everywhere

    backend = ("Anthropic" if os.environ.get("ANTHROPIC_API_KEY")
               else "GitHub Models" if os.environ.get("GITHUB_TOKEN") else "NONE")
    print(f"  · classify: backend={backend} model={stats['model']} calls={stats['calls']} "
          f"new={stats['new_ai']} from-cache={stats['cached']} keyword-guess={stats['rules']}")
    if stats["error"]:
        print(f"  !! classify stopped early: {stats['error'][:240]}")
    return stats
