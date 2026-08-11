#!/usr/bin/env python3
"""Entrypoint: read the company CSV, gather, score, draft, report.

    python run.py                          # input_example.csv, all modules
    python run.py --input my_list.csv      # your own Apollo export
    python run.py --limit 3                # smoke test
    python run.py --no-llm                 # rule-based scores, no openers
    python run.py --refresh                # ignore the page cache

Input CSV needs ``company`` and ``domain`` columns (a few header spellings
are accepted); ``industry``, ``contact_name``, ``contact_title``,
``contact_email`` and ``competitor_domain`` are optional and passed through
untouched. The tool never looks up people — the only personal data in the
output is whatever contact you supplied yourself.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
from pathlib import Path

import config
import personalize
import report
import score
from gather import competitive, entity, hiring, reviews, site
from llm import AnthropicEngine, LLMError
from model import CompanyResult
from net import PoliteClient, setup_logging

log = logging.getLogger("signal.run")

# Accepted header spellings -> canonical field. Apollo exports and hand-made
# lists disagree about almost every one of these.
HEADER_ALIASES = {
    "company": "company", "company name": "company", "name": "company",
    "organization": "company",
    "domain": "domain", "website domain": "domain", "website": "domain",
    "url": "domain", "site": "domain",
    "industry": "industry",
    "team size": "team_size", "team_size": "team_size", "employees": "team_size",
    "headcount": "team_size", "# employees": "team_size",
    "location": "location", "city": "location",
    "contact name": "contact_name", "contact_name": "contact_name",
    "first name": "contact_name",
    "contact title": "contact_title", "contact_title": "contact_title",
    "title": "contact_title",
    "contact email": "contact_email", "contact_email": "contact_email",
    "email": "contact_email",
    "competitor": "competitor_domain", "competitor domain": "competitor_domain",
    "competitor_domain": "competitor_domain",
}


def normalize_domain(raw: str) -> str:
    domain = raw.strip().lower()
    domain = re.sub(r"^https?://", "", domain)
    return domain.split("/")[0].strip()


def read_input(path: Path) -> list[CompanyResult]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise SystemExit(f"{path}: empty file")
        mapping = {}
        for header in reader.fieldnames:
            canonical = HEADER_ALIASES.get(header.strip().lower())
            if canonical and canonical not in mapping.values():
                mapping[header] = canonical
        if "company" not in mapping.values() or "domain" not in mapping.values():
            raise SystemExit(
                f"{path}: need at least a company and a domain column; "
                f"found headers {reader.fieldnames}")
        results = []
        for row in reader:
            fields = {canonical: (row.get(header) or "").strip()
                      for header, canonical in mapping.items()}
            fields["domain"] = normalize_domain(fields.get("domain", ""))
            if fields.get("competitor_domain"):
                fields["competitor_domain"] = normalize_domain(fields["competitor_domain"])
            if not fields.get("company") or not fields.get("domain"):
                log.warning("skipping row with missing company/domain: %r", row)
                continue
            results.append(CompanyResult(**fields))
        return results


def process(client: PoliteClient, engine: AnthropicEngine | None,
            result: CompanyResult, modules: dict[str, bool]) -> None:
    facts: dict = {"careers_url": None, "board_hints": [], "chat_widget": None,
                   "homepage_text": ""}
    if modules["site"]:
        facts = site.gather(client, result)
    else:
        result.coverage["site"] = "skipped"

    # The entity gate runs before anything else is gathered or scored: if this
    # is a trade association or a publisher, the rest of the pipeline is wasted
    # work and a wasted model call, and its output would be actively misleading.
    if modules["entity"]:
        entity.check(result, facts.get("homepage_text", ""), engine)
        if not result.is_target:
            result.band = "DISQUALIFIED"
            score.weigh_only(result)  # the observations stay honest; the score does not exist
            result.score = 0
            result.offer = "unclear"
            result.reasoning = (
                f"Not a prospect: this is a {result.entity_type.replace('_', ' ')}, "
                f"not an operating business of the target type. Evidence from its "
                f"own homepage: \"{result.entity_evidence}\"")
            return
        if result.entity_type == "unclear":
            result.band = "REVIEW"
            score.weigh_only(result)
            result.score = 0
            result.offer = "unclear"
            result.reasoning = ("Entity type could not be determined from the "
                                "homepage; review what this company is before "
                                "contacting it.")
            return
    else:
        result.entity_type = "target"

    if modules["hiring"]:
        hiring.gather(client, result, careers_url=facts.get("careers_url"),
                      board_hints=facts.get("board_hints"))
    else:
        result.coverage["hiring"] = "skipped"

    if modules["reviews"]:
        reviews.gather(client, result)
    else:
        result.coverage["reviews"] = "skipped"

    if modules["competitive"]:
        competitive.gather(client, result,
                           company_has_widget=bool(facts.get("chat_widget")))
    else:
        result.coverage["competitive"] = "skipped"

    score.score_company(result, engine)
    if engine is not None:
        personalize.draft_opener(engine, result)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Qualify a list of companies by public signals.")
    ap.add_argument("--input", type=Path, default=config.INPUT_PATH)
    ap.add_argument("--limit", type=int, default=0, help="process at most N companies")
    ap.add_argument("--no-llm", action="store_true",
                    help="rule-based scores only; skips openers")
    ap.add_argument("--refresh", action="store_true", help="bypass the page cache")
    ap.add_argument("--no-reviews", action="store_true")
    ap.add_argument("--no-hiring", action="store_true")
    ap.add_argument("--no-competitive", action="store_true")
    ap.add_argument("--no-entity", action="store_true",
                    help="skip the entity disqualifier (scores everything)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    setup_logging(config.LOG_DIR / "run.log", args.verbose)
    # Load .env here, not just inside the LLM engine: the Google Places key
    # must be visible even on --no-llm runs.
    config.load_dotenv()
    modules = dict(config.MODULES)
    if args.no_reviews:
        modules["reviews"] = False
    if args.no_hiring:
        modules["hiring"] = False
    if args.no_competitive:
        modules["competitive"] = False
    if args.no_entity:
        modules["entity"] = False

    results = read_input(args.input)
    if args.limit:
        results = results[: args.limit]
    if not results:
        log.error("no usable rows in %s", args.input)
        return 1

    engine = None
    if not args.no_llm:
        try:
            engine = AnthropicEngine()
        except LLMError as exc:
            log.error("%s", exc)
            return 1

    client = PoliteClient(cache_max_age=0 if args.refresh else 6 * 3600)
    started = time.time()
    log.info("processing %d compan(ies) from %s", len(results), args.input)

    for i, result in enumerate(results, 1):
        log.info("[%d/%d] %s (%s)", i, len(results), result.company, result.domain)
        try:
            process(client, engine, result, modules)
        except Exception:
            # A crash on one company must not cost the run; but it is loud,
            # scored 0, and marked as an error rather than "no signal".
            log.exception("unhandled failure on %s", result.company)
            result.coverage.setdefault("site", "unreachable")
            result.notes.append("run: unhandled error while processing; see logs/run.log")
            result.reasoning = result.reasoning or "Processing failed; not scored."
        log.info("      -> %d%% %s (%s), %d signal(s)",
                 result.score, result.band, result.offer, len(result.signals))

    meta = {
        "input": str(args.input.name),
        "finished_at": report.finished_at(),
        "model": engine.model if engine else "",
    }
    report.write_report(results, meta)
    report.write_csv(results)
    with config.RESULTS_JSONL_PATH.open("w", encoding="utf-8") as fh:
        for result in results:
            fh.write(json.dumps(result.as_record(), ensure_ascii=False) + "\n")

    bands = {b: sum(1 for r in results if r.band == b) for b in config.BANDS}
    summary = {
        "finished_at": meta["finished_at"],
        "input": meta["input"],
        "companies": len(results),
        "bands": bands,
        "signals_total": sum(len(r.signals) for r in results),
        "openers_drafted": sum(1 for r in results if r.opener),
        "partial_coverage_companies": sum(
            1 for r in results if any(s in report.FAILED_STATES
                                      for s in r.coverage.values())),
        "modules": modules,
        "http": client.stats,
        "llm": {
            "model": meta["model"],
            "calls": engine.calls if engine else 0,
            "cache_hits": engine.cache_hits if engine else 0,
            "input_tokens": engine.input_tokens if engine else 0,
            "output_tokens": engine.output_tokens if engine else 0,
        },
        "elapsed_seconds": round(time.time() - started, 1),
    }
    config.SUMMARY_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    log.info("done in %.0fs: %d PASS / %d MAYBE / %d FAIL; wrote %s, %s, %s",
             summary["elapsed_seconds"], bands["PASS"], bands["MAYBE"], bands["FAIL"],
             config.REPORT_PATH.name, config.RESULTS_PATH.name, config.SUMMARY_PATH.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
