# Daily labelling task — instructions for Claude

This file is the full instruction set for the scheduled daily session that labels
new events. The site owner approved this task; do exactly this and nothing else.

## What you are doing

The update workflow (every 6 hours) collects Berlin events and writes every event
that still lacks a proper label to `docs/needs_labels.json`. Each entry has:

- `key` — the event's original title (often German). Copy it **exactly**.
- `venue`, `site_category`, `date`
- `about` — the event's own description from its web page (may be empty)

Your job: give each entry an English title, a kind, an audience, and a short
summary, then merge and push.

## Steps

1. `cd` into the repo and `git pull --rebase origin main`.
2. Read `docs/needs_labels.json`. If it is `[]`, stop — nothing to do.
3. Read the rules in `sources/classify.py` (the `_PROMPT` text). Follow them
   exactly: allowed values for `kind` and `audience`, length limits, and above
   all: **the summary uses only what the title and `about` say. Never invent.**
   If there is nothing beyond the title, use `""` for summary.
4. Label in chunks of about 40 entries so you stay careful. Base the summary on
   `about` when it exists: what the thing is, who runs it, what you see or do.
   If `about` says registration is required, end with "Registration required."
5. Write all labels as one JSON array to `/tmp/labels.json`, objects shaped
   `{"key": ..., "title": ..., "kind": ..., "audience": ..., "summary": ...}`.
6. Run `python tools/apply_labels.py /tmp/labels.json`. It validates every label
   and merges it into `docs/.classify_cache.json`. Fix and re-run if it rejects any.
7. Commit **only** `docs/.classify_cache.json` with message
   `Daily labels: N events` and push to `main`. The push starts the update
   workflow, which rebuilds the site with the new labels.

## Rules

- Do not fetch event websites — the page text is already in `about`
  (gratis-in-berlin.de refuses bursts of requests).
- Do not change code, config, the site, or any other file.
- If the push is rejected, `git pull --rebase origin main` and push again once.
- If anything else fails, stop and leave the repo unchanged.
