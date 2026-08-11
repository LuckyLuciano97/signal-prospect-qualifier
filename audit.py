#!/usr/bin/env python3
"""Signal audit — which detectors actually fire, and on whom.

Run this before touching a weight. A signal that fires on nearly every
company is a constant, not a signal, and reweighting it changes nothing;
a signal that never fires is either a broken matcher or a niche mismatch,
and those have opposite fixes. Guessing which is which is how a scoring
table rots.

    python audit.py                       # audit results.jsonl
    python audit.py --results other.jsonl

For zero-fire signals the report lists the pages that *were* searched, so
"the phrase isn't there" can be told apart from "we never looked".
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys

import config


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Audit signal detection across a run.")
    ap.add_argument("--results", type=pathlib.Path, default=config.RESULTS_JSONL_PATH)
    args = ap.parse_args(argv)

    if not args.results.exists():
        print(f"{args.results} not found - run run.py first")
        return 1
    records = [json.loads(line) for line in
               args.results.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not records:
        print("no records")
        return 1

    fired: dict[str, list[str]] = collections.defaultdict(list)
    detail_counts: collections.Counter = collections.Counter()
    total_evidence = 0
    pages_seen: set[str] = set()
    for record in records:
        for signal in record["signals"]:
            fired[signal["type"]].append(record["company"])
            total_evidence += 1
            # Collapse the variable part of a detail so near-identical lines
            # group: "no client login ... across 4 page(s)" == the 5-page one.
            detail_counts[signal["detail"].split(" (")[0][:60]] += 1
        if record.get("coverage", {}).get("site") == "ok":
            pages_seen.add(record["company"])

    companies = len(records)
    print(f"Signal audit over {companies} compan(ies), {total_evidence} evidence line(s)")
    print(f"source: {args.results.name}\n")

    print(f"{'signal type':<26} {'fired':>5} {'companies':>10} {'% of evidence':>14}  weight")
    print("-" * 78)
    for signal_type, weight in sorted(config.SIGNAL_WEIGHTS.items(),
                                      key=lambda kv: -kv[1]):
        hits = fired.get(signal_type, [])
        share = (len(hits) / total_evidence * 100) if total_evidence else 0
        on = len(set(hits))
        flag = ""
        if on >= companies * 0.8 and companies >= 5:
            flag = "  <- constant, not a signal"
        elif not hits:
            flag = "  <- never fired"
        print(f"{signal_type:<26} {len(hits):>5} {on:>10} {share:>13.1f}%  {weight:>6}{flag}")

    print(f"\nMost repeated evidence lines (the run's actual vocabulary):")
    for detail, count in detail_counts.most_common(8):
        print(f"  {count:>3}x  {detail}")

    silent = [s for s in config.SIGNAL_WEIGHTS if not fired.get(s)]
    if silent:
        print(f"\nZero-fire signals: {', '.join(silent)}")
        print(f"  {len(pages_seen)} of {companies} compan(ies) had a readable site, so "
              f"for those the pages were searched and the pattern was absent.")
        print("  Absent pattern on readable pages = niche mismatch (fix the niche "
              "playbook).\n  Absent pages = coverage problem (fix the crawl). "
              "Do not reweight either one.")

    worst = max(((t, len(h)) for t, h in fired.items()), key=lambda kv: kv[1],
                default=("", 0))
    if total_evidence and worst[1] / total_evidence > 0.4:
        print(f"\nDIVERSITY WARNING: '{worst[0]}' is {worst[1] / total_evidence:.0%} "
              f"of all evidence in this run (limit 40%).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
