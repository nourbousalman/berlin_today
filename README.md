# Berlin events — one page, auto-updated

A self-refreshing web page that pulls Berlin events into a single, filterable
timetable (free-only toggle, "this weekend" view, category filters, search).
No server to run and no hosting bill: a scheduled **GitHub Action** rebuilds the
data a few times a day and **GitHub Pages** serves the page.

**Two sections, toggled at the top of the page:**
- **One-off** — dated events (concerts, screenings, openings), grouped by day, with the "This weekend / Today / 7 days / All" filter.
- **Recurring** — the weekly and monthly regulars (jams, parkrun, language exchange…), grouped by day of the week. Anything with an iCal repeat rule (RRULE) is detected as recurring automatically; you can also force a whole feed into this section with `recurring: true`.
- **Directory** — a curated list of Berlin's best listings sites, jam directories, sport programmes, markets and more (the resources to check by hand for things no feed covers).

**Also on the page:** a **Cost** filter (Any / Free / ≤€5), **Sort** by time or price, and a **Sources** panel at the bottom showing how many events each feed returned (a `0` means that feed is empty or broken — your at-a-glance health check). German titles and descriptions are **auto-translated to English** in the Action (cached; falls back to the original if translation is unavailable).

**Free/cheap only:** the aggregator drops anything over **€5** (set by `max_price` in config) and anything known to be paid without a stated price. Resident Advisor is **off** by default for this reason — it's paid clubs and its API doesn't expose prices, so every event came through as "ticketed" with no amount. Flip `resident_advisor.enabled` to `true` if you ever want paid nightlife back.

```
sources ──► aggregate.py ──► docs/events.json ──► docs/index.html (the page)
   ▲                                 ▲
GitHub Action (cron, every 6h) ──────┘
```

---

## Set it up (~10 minutes, one time)

1. **Create a GitHub repo** and push these files to it (branch `main`).
   ```bash
   git init && git add . && git commit -m "init"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
2. **Enable Pages:** repo → *Settings → Pages* → *Source: Deploy from a branch* →
   Branch **main**, folder **/docs** → Save. Your page appears at
   `https://<you>.github.io/<repo>/` within a minute.
3. **Enable Actions:** repo → *Actions* tab → enable workflows. Then open
   *Update events* → *Run workflow* once to populate real data immediately.
   After that it runs itself every 6 hours.

That's it. Bookmark the Pages URL — that's your one place.

---

## Add your own sources (no code needed)

Everything is driven by **`config.yaml`**. The two feed types:

- **`ics_feeds`** — iCal / `.ics` links. *This is the robust one.* Almost any
  venue or org with an "Add to calendar / Subscribe" button exposes one; a public
  Google Calendar gives you an iCal URL under *Settings → Integrate calendar →
  Public address in iCal format*. Tag each feed with a `category` and an
  `is_free` default.
- **`rss_feeds`** — RSS/Atom links. Handy but weaker: many event RSS feeds carry
  only a publish date, not the event's real date. Prefer iCal where a site offers
  both.

```yaml
ics_feeds:
  - name: "SO36"
    url: "https://…/events.ics"
    category: "music"      # music | nightlife | art | sport | community | market | other
    is_free: false
```

Delete the bundled **`Demo`** feed once you've added real ones. Then either wait
for the next scheduled run or trigger *Run workflow*.

---

## Sources & how much to trust each

| Source | Status | Reliability |
|---|---|---|
| **Resident Advisor** (`resident_advisor.py`) | On by default. Berlin clubs/electronic via ra.co's public GraphQL API. | Structured and solid, but **unofficial** — verify the Berlin area id (`34`) at `ra.co/events/de/berlin`, and if it ever returns 403/empty in the Action, its request headers may need a refresh. |
| **iCal feeds** (`ics_feeds.py`) | You add them. | **Most robust** — iCal is a fixed standard; these rarely break. |
| **RSS feeds** (`rss_feeds.py`) | You add them. | OK; event dates can be imprecise. |
| **HTML scrapers** (`html_scrapers.py`) | **Off** by default. | **Fragile** — breaks silently when a site changes its markup. Opt-in template only; check the site's robots.txt/terms first. |

Every source runs in its own `try/except`, so if one breaks (a dead feed, an RA
change) it logs a warning and the rest of the page still fills in. Not the whole
thing going dark.

---

## Run it locally

```bash
pip install -r requirements.txt
python aggregate.py            # writes docs/events.json
cd docs && python -m http.server 8000   # open http://localhost:8000
```

---

## Files

```
aggregate.py            orchestrates all sources → docs/events.json
config.yaml             your sources + settings (edit this)
sources/
  base.py               Event schema, category mapping, de-dup
  resident_advisor.py   RA GraphQL (nightlife) — works out of the box
  ics_feeds.py          generic iCal ingester (robust; extend here)
  rss_feeds.py          generic RSS/Atom ingester
  html_scrapers.py      optional, fragile, off by default
docs/
  index.html            the page (loads events.json, filters client-side)
  events.json           generated data
.github/workflows/update.yml   the scheduler
sample.ics              demo calendar so the page isn't empty on day one
```

---

## Honest limits

- **It's only as complete as its sources.** Out of the box you get RA (clubs) +
  the demo feed. The page gets genuinely useful as you add a handful of iCal
  feeds for the things you care about — that's the 10-minute-a-once setup that
  pays off weekly.
- **RA is the one source I couldn't fully test** from where this was built (no
  network access to ra.co), so confirm it on your first *Run workflow*. The
  iCal/RSS pipeline is tested and working.
- **Free-ness isn't always known.** RA doesn't expose whether an event is free,
  so those show as ticketed/unknown; iCal/RSS events use the `is_free` you set
  per feed. The "Free only" filter shows only events explicitly marked free.
- **Scraping is opt-in for a reason** — see the table above.
