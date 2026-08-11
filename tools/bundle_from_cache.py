#!/usr/bin/env python3
"""Emit crawl bundles from the local page cache, in the n8n hand-off format.

This is the fixture generator for bundle mode, and it is how the contract in
``sources/crawl_bundle.py`` is proved without an n8n instance in the loop: it
produces exactly the JSON an n8n crawl is asked to produce, from pages this
program already fetched, so a bundle-mode run can be compared field-by-field
against the live run it was built from. If the two agree, the boundary holds.

    python tools/bundle_from_cache.py --input input_example.csv --out bundles/

It is also useful on its own: it turns any completed live run into a
reproducible offline corpus that scores identically without touching a
website again.
"""

from __future__ import annotations

import argparse
import csv
import json
import pathlib
import re
import sys
import time
from urllib.parse import urlsplit

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config
from gather import visible_text

PAGE_TYPES = {
    "about": re.compile(r"/about|/company|who-we-are", re.I),
    "services": re.compile(r"/services|/products|/solutions|/platform", re.I),
    "careers": re.compile(r"/careers|/jobs|join-us", re.I),
    "contact": re.compile(r"/contact", re.I),
}


def classify(url: str) -> str:
    path = urlsplit(url).path.rstrip("/")
    if not path or path == "":
        return "homepage"
    for page_type, pattern in PAGE_TYPES.items():
        if pattern.search(path):
            return page_type
    return "other"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build crawl bundles from the page cache.")
    ap.add_argument("--input", type=pathlib.Path, default=config.INPUT_PATH)
    ap.add_argument("--out", type=pathlib.Path, default=config.REPO / "bundles")
    ap.add_argument("--cache", type=pathlib.Path, default=config.CACHE_DIR)
    args = ap.parse_args(argv)

    with args.input.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        print(f"{args.input}: no rows")
        return 1

    # Index every cached response by host.
    by_host: dict[str, list[dict]] = {}
    for path in args.cache.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        url = payload.get("url", "")
        if "robots.txt" in url or "#" in url:
            continue
        host = urlsplit(url).netloc.lower().removeprefix("www.")
        if host:
            by_host.setdefault(host, []).append(payload)

    args.out.mkdir(parents=True, exist_ok=True)
    written = 0
    for row in rows:
        company = (row.get("company") or "").strip()
        domain = (row.get("domain") or "").strip().lower().removeprefix("www.")
        if not company or not domain:
            continue
        cached = by_host.get(domain, [])

        pages, reached, failed = [], [], []
        # Every cached 200 goes in, including two pages of the same type: a
        # site with /about and /about-us has both, a crawler would send both,
        # and dropping one changed a "pages checked" count between modes.
        for payload in sorted(cached, key=lambda p: len(p.get("url", ""))):
            status = int(payload.get("status", 0))
            page_type = classify(payload["url"])
            if status != 200:
                failed.append({"page_type": page_type,
                               "reason": f"HTTP {status}"})
                continue
            html = payload.get("body", "")
            pages.append({
                "url": payload["url"],
                "page_type": page_type,
                "status": 200,
                "fetched_at": payload.get("fetched_at", ""),
                "html": html,
                "text": visible_text(html),
            })
            reached.append(page_type)

        attempted = sorted({"homepage", "about", "contact", "careers", "services"}
                           | set(reached))
        for page_type in attempted:
            if page_type not in reached and \
                    not any(f["page_type"] == page_type for f in failed):
                failed.append({"page_type": page_type, "reason": "not reached"})

        bundle = {
            "company": company,
            "domain": domain,
            "industry": (row.get("industry") or "").strip(),
            "team_size": (row.get("team_size") or "").strip(),
            "crawled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "robots": {"fetched": True, "allows_crawl": True, "disallowed_paths": []},
            "pages": pages,
            "coverage": {
                "attempted": attempted,
                "reached": sorted(set(reached)),
                "failed": failed,
                # Review sources are the crawler's business, not this tool's;
                # it reports only what it actually knows.
                "blocked": [],
            },
        }
        slug = re.sub(r"[^a-z0-9]+", "-", domain).strip("-")
        (args.out / f"{slug}.json").write_text(
            json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
        written += 1
        print(f"{slug}.json  {len(pages)} page(s), reached: "
              f"{', '.join(sorted(set(reached))) or 'none'}")

    print(f"\nwrote {written} bundle(s) to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
