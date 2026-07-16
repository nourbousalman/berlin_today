"""Verify every entry in directory.json: URL health + feed discovery.

Runs in GitHub Actions (which has full internet access). For each entry it:
  1. GETs the URL (follows redirects, browser User-Agent, generous timeout)
  2. records status, final URL (catches moved/renamed sites), and page <title>
  3. hunts for an events feed:
       - <link rel="alternate"> RSS/Atom declared in the HTML head
       - .ics / webcal links anywhere on the page
       - embedded Google Calendar
       - probes common paths: /feed/, /rss, /events/feed/, /?feed=rss2,
         /events/?ical=1  (only if nothing was declared)
  4. classifies each entry:  auto  = a usable feed exists → we can wire it in
                             manual = nothing machine-readable → browse by hand

Writes docs/directory_verified.json + docs/DIRECTORY_REPORT.md and prints a summary.
Nothing here modifies the event pipeline; it's a standalone audit.
"""
from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

ROOT = Path(__file__).parent
DIRECTORY = ROOT / "directory.json"
OUT_JSON = ROOT / "docs" / "directory_verified.json"
OUT_MD = ROOT / "docs" / "DIRECTORY_REPORT.md"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
           "Accept-Language": "en,de;q=0.9"}
TIMEOUT = 25

FEED_PROBES = ["/feed/", "/rss", "/events/feed/", "/?feed=rss2", "/events/?ical=1",
               "/veranstaltungen/feed/", "/termine/feed/"]

RSS_LINK = re.compile(
    r'<link[^>]+rel=["\']alternate["\'][^>]*type=["\']application/(?:rss|atom)\+xml["\'][^>]*>',
    re.I)
HREF = re.compile(r'href=["\']([^"\']+)["\']', re.I)
ICS_LINK = re.compile(r'href=["\']([^"\']*(?:\.ics|webcal://|ical=1|/ical/)[^"\']*)["\']', re.I)
GCAL = re.compile(r'calendar\.google\.com/calendar/(?:embed|ical)', re.I)
TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)

# Free-ness hints (used only as a soft signal for the "always free" tab)
FREE_HINT = re.compile(
    r"\b(eintritt frei|freier eintritt|free entry|free admission|kostenlos|gratis|"
    r"admission is free|entry is free)\b", re.I)


def looks_like_feed(text: str) -> bool:
    head = text[:600].lower()
    return ("<rss" in head or "<feed" in head or "begin:vcalendar" in head
            or "<?xml" in head and ("rss" in head or "atom" in head))


def probe(session: requests.Session, url: str) -> tuple[bool, str]:
    try:
        r = session.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        if r.status_code == 200 and looks_like_feed(r.text):
            return True, url
    except Exception:
        pass
    return False, ""


def check(entry: dict) -> dict:
    url = entry["url"]
    res = dict(entry)
    res.update(status=None, final_url=None, title=None, ok=False,
               feed=None, feed_kind=None, source_status="manual",
               free_hint=False, error=None)

    session = requests.Session()
    try:
        r = session.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        res["status"] = r.status_code
        res["final_url"] = r.url
        res["ok"] = r.status_code < 400
        html = r.text if "text" in r.headers.get("content-type", "") or r.text[:1] == "<" else ""
    except Exception as exc:
        res["error"] = f"{type(exc).__name__}: {exc}"[:120]
        return res

    if not res["ok"] or not html:
        return res

    m = TITLE.search(html)
    if m:
        res["title"] = re.sub(r"\s+", " ", m.group(1)).strip()[:120]
    res["free_hint"] = bool(FREE_HINT.search(html))

    # 1) declared RSS/Atom
    for tag in RSS_LINK.findall(html):
        href = HREF.search(tag)
        if href:
            res["feed"] = urljoin(r.url, href.group(1))
            res["feed_kind"] = "rss"
            break

    # 2) iCal / webcal links on the page
    if not res["feed"]:
        ics = ICS_LINK.search(html)
        if ics:
            res["feed"] = urljoin(r.url, ics.group(1))
            res["feed_kind"] = "ical"

    # 3) embedded Google Calendar
    if not res["feed"] and GCAL.search(html):
        res["feed"] = "google-calendar-embed"
        res["feed_kind"] = "gcal"

    # 4) probe common feed paths
    if not res["feed"]:
        base = f"{urlparse(r.url).scheme}://{urlparse(r.url).netloc}"
        for path in FEED_PROBES:
            found, furl = probe(session, base + path)
            if found:
                res["feed"] = furl
                res["feed_kind"] = "rss"
                break

    res["source_status"] = "auto" if res["feed"] else "manual"
    return res


def main() -> int:
    entries = json.loads(DIRECTORY.read_text(encoding="utf-8"))
    print(f"Verifying {len(entries)} directory entries…", flush=True)

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(check, entries))
    results.sort(key=lambda e: e["id"])

    ok = [r for r in results if r["ok"]]
    dead = [r for r in results if not r["ok"]]
    moved = [r for r in ok
             if r["final_url"] and r["final_url"].rstrip("/") != r["url"].rstrip("/")
             and urlparse(r["final_url"]).netloc.replace("www.", "")
                 != urlparse(r["url"]).netloc.replace("www.", "")]
    auto = [r for r in results if r["source_status"] == "auto"]

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    lines = [f"# Directory verification report",
             "",
             f"- checked: **{len(results)}**",
             f"- reachable: **{len(ok)}**",
             f"- dead / unreachable: **{len(dead)}**",
             f"- domain changed (moved): **{len(moved)}**",
             f"- feeds found (auto): **{len(auto)}**",
             f"- no feed (manual check): **{len(results) - len(auto)}**",
             "", "---", "", "## Dead or unreachable", ""]
    for r in dead:
        lines.append(f"- **{r['id']}. {r['name']}** — {r['url']} → "
                     f"`{r['status'] or r['error']}`")
    lines += ["", "## Moved (update these URLs)", ""]
    for r in moved:
        lines.append(f"- **{r['id']}. {r['name']}** — {r['url']} → **{r['final_url']}**")
    lines += ["", "## Feeds found (wire these in)", ""]
    for r in auto:
        lines.append(f"- **{r['id']}. {r['name']}** — `{r['feed_kind']}` → {r['feed']}")
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")

    print(f"\n{'='*60}")
    print(f"  checked {len(results)} in {time.time()-t0:.0f}s")
    print(f"  reachable      : {len(ok)}")
    print(f"  dead           : {len(dead)}")
    print(f"  moved          : {len(moved)}")
    print(f"  feeds found    : {len(auto)}   <-- can be wired in")
    print(f"  manual check   : {len(results)-len(auto)}")
    print(f"{'='*60}\n")
    print(f"Wrote {OUT_JSON.relative_to(ROOT)} and {OUT_MD.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
