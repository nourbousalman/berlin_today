#!/usr/bin/env python3
"""Merge labels written by the daily Claude task into docs/.classify_cache.json.

Input: a JSON array of objects
    {"key": <original title, exactly as in docs/needs_labels.json>,
     "title": ..., "kind": ..., "audience": ..., "summary": ...}

Every object is validated exactly like model output in the pipeline
(sources.classify._clean): wrong kinds/audiences are rejected, filler or
title-echo summaries are blanked. Labels for keys that had page text in
needs_labels.json are marked ctx=True so they are not redone.

Usage:  python tools/apply_labels.py labels.json
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from sources.classify import _clean, _key  # noqa: E402

CACHE = ROOT / "docs" / ".classify_cache.json"
NEEDS = ROOT / "docs" / "needs_labels.json"


def main(path: str) -> int:
    labels = json.loads(Path(path).read_text("utf-8"))
    cache = json.loads(CACHE.read_text("utf-8")) if CACHE.exists() else {}
    needs = {n["key"]: n for n in json.loads(NEEDS.read_text("utf-8"))} if NEEDS.exists() else {}
    ok = bad = unknown = 0
    applied = set()
    for row in labels:
        key = _key(row.get("key", ""))
        if not key:
            bad += 1
            continue
        if needs and key not in needs:
            unknown += 1          # still accepted, but worth knowing about
        clean = _clean(row, key)
        if not clean:
            bad += 1
            print(f"  rejected: {key[:70]!r} kind={row.get('kind')!r} audience={row.get('audience')!r}")
            continue
        clean["ctx"] = bool(needs.get(key, {}).get("about"))
        clean["by"] = "claude-task"
        cache[key] = clean
        applied.add(key)
        ok += 1
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0), "utf-8")
    left = [n for k, n in needs.items() if k not in applied]
    print(f"applied {ok}, rejected {bad}, not-in-needs {unknown}; "
          f"still unlabelled: {len(left)}; cache now {len(cache)}")
    return 0 if ok or not labels else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
